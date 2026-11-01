"""Configuration I/O for schema-first TOML settings."""

from __future__ import annotations

import contextlib
import hashlib
from importlib import resources
import os
import stat
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from typing import BinaryIO, Callable, Iterator

import tomlkit
from pydantic import ValidationError
from tomlkit.container import OutOfOrderTableProxy
from tomlkit.items import Table
from tomlkit.toml_document import TOMLDocument

from checksi.config_schema import AppConfig, ErrorSeverity
from checksi.errors.defs import AnalysisErrorCode


def user_config_path() -> Path:
    """Return the per-user settings destination without creating it."""
    try:
        if sys.platform == "win32":
            candidate = os.environ.get("LOCALAPPDATA")
            base = Path(candidate) if candidate else None
            if base is None or not base.is_absolute():
                base = Path.home() / "AppData" / "Local"
            return base / "CheckSI" / "settings.toml"
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Application Support" / "CheckSI" / "settings.toml"
        candidate = os.environ.get("XDG_CONFIG_HOME")
        base = Path(candidate) if candidate else None
        if base is None or not base.is_absolute():
            base = Path.home() / ".config"
        return base / "checksi" / "settings.toml"
    except (OSError, RuntimeError) as exc:
        raise ConfigIOError(
            "Unable to determine the per-user settings path; select a settings file with --config-file. "
            f"{exc}"
        ) from exc


def resolve_config_paths(config: AppConfig, base_dir: Path | None = None) -> AppConfig:
    """Resolve a copy against an explicit source directory or invocation CWD."""
    base = Path(os.path.abspath(os.path.normpath(base_dir or Path.cwd())))
    resolved = config.model_copy(deep=True)
    for key in ("input_path", "output_dir"):
        try:
            path = getattr(resolved.paths, key).expanduser()
        except RuntimeError as exc:
            raise ConfigValidationError(f"Invalid paths.{key}: {exc}") from exc
        # Keep a symlink's logical filename: extraction and output naming use its
        # suffix and stem, even when its storage target has no document extension.
        setattr(resolved.paths, key, Path(os.path.abspath(os.path.normpath(base / path))))
    executable = resolved.conversion.libreoffice_path
    if executable:
        try:
            path = Path(executable).expanduser()
        except RuntimeError as exc:
            raise ConfigValidationError(f"Invalid conversion.libreoffice_path: {exc}") from exc
        if path.is_absolute() or os.path.dirname(executable):
            resolved.conversion.libreoffice_path = str((base / path).resolve())
    return resolved


class ConfigError(RuntimeError):
    """Base class for configuration I/O and validation errors."""


class ConfigValidationError(ConfigError):
    """Raised when loaded TOML does not satisfy the AppConfig schema."""


class ConfigIOError(ConfigError):
    """Raised when configuration files cannot be read or written safely."""


class ConfigConflictError(ConfigError):
    """Raised when a settings file changed since this client observed it."""


class ConfigManager:
    """Read and write AppConfig instances from TOML with atomic writes."""

    def __init__(self, config_path: str | Path | None = None) -> None:
        self._config_path = Path(config_path).expanduser().resolve() if config_path is not None else None
        self._allow_factory = config_path is None
        self._loaded_factory = False
        self._revision_observed = False
        self._loaded_revision: str | None = None
        self.migration_notices: tuple[str, ...] = ()

    @property
    def config_path(self) -> Path:
        """Resolve the user location only when an operation needs user settings."""
        if self._config_path is None:
            self._config_path = user_config_path().expanduser().resolve()
        return self._config_path

    @property
    def uses_factory_defaults(self) -> bool:
        """Whether the last load used invocation-relative packaged defaults."""
        return self._loaded_factory

    @staticmethod
    def load_factory() -> AppConfig:
        """Load immutable packaged defaults without accessing user settings."""
        try:
            text = resources.files("checksi").joinpath("defaults.toml").read_text(encoding="utf-8")
            return AppConfig.model_validate(tomllib.loads(text))
        except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
            raise ConfigIOError(f"Unable to load packaged default settings: {exc}") from exc

    def resolve_paths(self, config: AppConfig) -> AppConfig:
        """Resolve factory paths at invocation CWD and file paths beside that file."""
        return resolve_config_paths(config, None if self._loaded_factory else self.config_path.parent)

    @property
    def lock_path(self) -> Path:
        """Lock file beside the real settings file, shared by every symlinked path."""
        target = Path(os.path.realpath(self.config_path))
        return target.parent / f"{target.name}.lock"

    def load(self) -> AppConfig:
        """Load TOML and remember its revision without writing any files."""
        try:
            with self._file_lock(exclusive=False):
                return self._load_unlocked()
        except OSError as exc:
            raise ConfigIOError(f"Unable to read configuration file {self.config_path}: {exc}") from exc

    def _load_unlocked(self) -> AppConfig:
        self.migration_notices = ()
        self._loaded_factory = False
        content = self._read_content()
        if content is None:
            if not self._allow_factory:
                raise ConfigIOError(f"Configuration file not found: {self.config_path}")
            config = self.load_factory()
            self._loaded_factory = True
        else:
            try:
                raw_data = tomllib.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
                raise ConfigValidationError(f"Invalid TOML in {self.config_path}: {exc}") from exc
            notices: list[str] = []
            try:
                config = AppConfig.model_validate(raw_data, context={"migration_notices": notices})
            except ValidationError as exc:
                details = "; ".join(
                    f"{'.'.join(str(part) for part in error['loc']) or 'configuration'}: {error['msg']}"
                    for error in exc.errors(include_input=False, include_url=False, include_context=False)[:8]
                )
                raise ConfigValidationError(f"Invalid configuration data in {self.config_path}: {details}") from exc
            added = [code for code in (AnalysisErrorCode.NMR_CONFIGURATION_DEPENDENT,
                                      AnalysisErrorCode.DP5_CONFIGURATION_DEPENDENT)
                     if code not in config.errors.overrides]
            for code in added:
                config.errors.overrides[code] = ErrorSeverity.WARNING
            if added:
                notices.append('Configuration-dependent NMR/DP5q diagnostics without explicit overrides '
                               'use warning severity in memory: ' + ', '.join(code.value for code in added)
                               + '. The settings file is unchanged until explicitly saved.')
            self.migration_notices = tuple(notices)
        self._remember_revision(content)
        return config

    def observe_revision(self) -> bool:
        """Remember a save destination before overwrite consent; return whether it exists."""
        try:
            with self._file_lock(exclusive=False):
                content = self._read_content()
                self._remember_revision(content)
            return content is not None
        except OSError as exc:
            raise ConfigIOError(f"Unable to read configuration file {self.config_path}: {exc}") from exc

    def _read_content(self) -> bytes | None:
        try:
            return self.config_path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ConfigIOError(f"Unable to read configuration file {self.config_path}: {exc}") from exc

    @staticmethod
    def _revision(content: bytes | None) -> str | None:
        return hashlib.sha256(content).hexdigest() if content is not None else None

    def _remember_revision(self, content: bytes | None) -> None:
        self._loaded_revision = self._revision(content)
        self._revision_observed = True

    def save(self, config: AppConfig) -> None:
        """Save atomically, checking the last successful load, observation or save if any."""
        validated = self._validate_for_save(config)
        try:
            with self._file_lock(exclusive=True):
                if self._revision_observed and self._revision(self._read_content()) != self._loaded_revision:
                    raise ConfigConflictError(
                        f"Configuration changed on disk: {self.config_path}. "
                        "Your edits have not been saved. Reload the latest settings before saving again, "
                        "or save your edits to a different file."
                    )
                self._save_unlocked(validated)
        except OSError as exc:
            raise ConfigIOError(f"Unable to write configuration file {self.config_path}: {exc}") from exc

    def update(self, change: Callable[[AppConfig], None]) -> AppConfig:
        """Read, modify, validate and save under one lock, returning the saved settings."""
        try:
            if not self._allow_factory and self._read_content() is None:
                raise ConfigIOError(f"Configuration file not found: {self.config_path}")
            with self._file_lock(exclusive=True):
                config = self._load_unlocked()
                change(config)
                validated = self._validate_for_save(config)
                self._save_unlocked(validated)
                return validated
        except OSError as exc:
            raise ConfigIOError(f"Unable to write configuration file {self.config_path}: {exc}") from exc

    def _validate_for_save(self, config: AppConfig) -> AppConfig:
        if not isinstance(config, AppConfig):
            raise TypeError("save() expects an AppConfig instance")
        validated = AppConfig.model_validate(config.model_dump())
        return self.resolve_paths(validated) if self._loaded_factory else validated

    def _save_unlocked(self, config: AppConfig) -> None:
        document = self._load_existing_document()
        rendered = tomlkit.dumps(self._build_document(document, config))
        self._atomic_write(rendered)
        self._remember_revision(rendered.encode("utf-8"))
        self._loaded_factory = False

    def _load_existing_document(self) -> TOMLDocument | None:
        if not self.config_path.exists():
            return None

        try:
            content = self.config_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigIOError(f"Unable to read existing configuration text: {exc}") from exc

        if not content.strip():
            return None

        try:
            return tomlkit.parse(content)
        except Exception as exc:
            raise ConfigValidationError(
                f"Unable to parse existing TOML structure in {self.config_path}"
            ) from exc

    def _build_document(
        self,
        document: TOMLDocument | None,
        config: AppConfig,
    ) -> TOMLDocument:
        doc = document if document is not None else self._new_document()

        # A table written as root dotted keys cannot gain a section header where
        # it stands: [[solvents.entries]] or [errors.overrides] there would claim
        # every root key that follows. Such tables are rebuilt at the end.
        if config.solvents.entries and self._is_dotted(doc, "solvents"):
            del doc["solvents"]
        if self._is_dotted(doc, "errors") and not isinstance(
            doc["errors"].get("overrides"), (Table, OutOfOrderTableProxy)
        ):
            del doc["errors"]

        paths_table = self._table_at(doc, ("paths",))
        paths_table["input_path"] = str(config.paths.input_path)
        paths_table["output_dir"] = str(config.paths.output_dir)
        self._prune_table(doc, ("paths",), {"input_path", "output_dir"})

        conversion_table = self._table_at(doc, ("conversion",))
        if config.conversion.libreoffice_path is not None:
            conversion_table["libreoffice_path"] = config.conversion.libreoffice_path
        else:
            conversion_table.pop("libreoffice_path", None)
        conversion_table["timeout_seconds"] = config.conversion.timeout_seconds
        self._prune_table(doc, ("conversion",), {"libreoffice_path", "timeout_seconds"})

        modes_table = self._table_at(doc, ("modes",))
        modes_table["deep_name_analysis"] = config.modes.deep_name_analysis.value
        modes_table["output_mode"] = config.modes.output_mode.value
        modes_table["open_reports_on_success"] = config.modes.open_reports_on_success
        modes_table["header_margin"] = config.modes.header_margin
        modes_table["footer_margin"] = config.modes.footer_margin
        self._prune_table(
            doc,
            ("modes",),
            {
                "deep_name_analysis",
                "output_mode",
                "open_reports_on_success",
                "header_margin",
                "footer_margin",
            },
        )

        dp5_table = self._table_at(doc, ("dp5",))
        dp5_table["enabled"] = config.dp5.enabled
        if "score_threshold_percent" not in dp5_table and "probability_threshold_percent" in dp5_table:
            dp5_table["score_threshold_percent"] = dp5_table["probability_threshold_percent"]
        dp5_table["score_threshold_percent"] = config.dp5.score_threshold_percent
        dp5_table["timeout_seconds"] = config.dp5.timeout_seconds
        dp5_table["keep_artifacts"] = config.dp5.keep_artifacts
        self._prune_table(
            doc,
            ("dp5",),
            {
                "enabled",
                "score_threshold_percent",
                "timeout_seconds",
                "keep_artifacts",
            },
        )

        solvents_table = self._table_at(doc, ("solvents",))
        # An empty array of tables renders as nothing, which would drop the
        # required field; write an explicit empty array instead.
        solvent_entries = tomlkit.aot() if config.solvents.entries else tomlkit.array()
        for entry in config.solvents.entries:
            row = tomlkit.table()
            row["name"] = entry.name
            row["aliases"] = list(entry.aliases)
            row["proton_shifts"] = list(entry.proton_shifts)
            row["carbon_shifts"] = list(entry.carbon_shifts)
            solvent_entries.append(row)
        solvents_table["entries"] = solvent_entries
        self._prune_table(doc, ("solvents",), {"entries"})

        self._table_at(doc, ("errors",))["default_severity"] = config.errors.default_severity.value
        desired_keys = {error_code.value for error_code in config.errors.overrides}
        self._prune_table(doc, ("errors", "overrides"), desired_keys)
        overrides_table = self._table_at(doc, ("errors", "overrides"))

        error_order = {
            error_code: index for index, error_code in enumerate(AnalysisErrorCode)
        }
        for error_code, severity in sorted(
            config.errors.overrides.items(),
            key=lambda item: (error_order[item[0]], item[0].value),
        ):
            overrides_table[error_code.value] = severity.value

        self._prune_table(doc, ("errors",), {"default_severity", "overrides"})
        self._prune_document(doc, {"paths", "conversion", "modes", "dp5", "solvents", "errors"})

        return doc

    @staticmethod
    def _new_document() -> TOMLDocument:
        doc = tomlkit.document()
        doc.add(tomlkit.comment("CheckSI configuration"))
        doc.add(
            tomlkit.comment(
                "Schema-first settings used by both CLI and GUI. Values are validated by Pydantic."
            )
        )
        doc.add(tomlkit.nl())
        return doc

    @staticmethod
    def _is_dotted(document: TOMLDocument, key: str) -> bool:
        """Whether a root table is written, at least in part, as dotted keys."""
        return any(name is not None and name.key == key and name.is_dotted() for name, _ in document.body)

    @staticmethod
    def _get_or_create_table(
        container: TOMLDocument | Table | OutOfOrderTableProxy, key: str
    ) -> Table | OutOfOrderTableProxy:
        current = container.get(key)
        # A table spelled with several dotted keys parses as a proxy; replacing
        # it would nest the original dotted keys inside a new [key] header.
        if isinstance(current, (Table, OutOfOrderTableProxy)):
            return current

        created = tomlkit.table()
        if current is not None:
            # Replacing an inline table in place would put a [key] header above
            # any root-level dotted keys that follow it and swallow them.
            del container[key]
        container[key] = created
        return created

    @classmethod
    def _table_at(cls, document: TOMLDocument, path: tuple[str, ...]) -> Table | OutOfOrderTableProxy:
        """Resolve a table from the document root, creating missing levels."""
        container = document
        for key in path:
            container = cls._get_or_create_table(container, key)
        return container

    @classmethod
    def _prune_table(cls, document: TOMLDocument, path: tuple[str, ...], allowed_keys: set[str]) -> None:
        # Re-resolve before every deletion: tomlkit's proxy for a table written
        # as dotted keys keeps stale indices after a delete and then fails.
        while True:
            table = cls._table_at(document, path)
            extra = next((key for key in table.keys() if key not in allowed_keys), None)
            if extra is None:
                return
            del table[extra]

    @staticmethod
    def _prune_document(document: TOMLDocument, allowed_keys: set[str]) -> None:
        for key in list(document.keys()):
            if key not in allowed_keys:
                del document[key]

    def _atomic_write(self, content: str) -> None:
        # Replace the file a symlink points to, so the link itself survives.
        target = Path(os.path.realpath(self.config_path))
        target.parent.mkdir(parents=True, exist_ok=True)
        existing_mode = None
        with contextlib.suppress(FileNotFoundError):
            existing_mode = stat.S_IMODE(os.stat(target).st_mode)
        fd, temp_path = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            text=True,
        )

        try:
            if existing_mode is not None and os.name != "nt":
                # Best effort: some file systems do not support mode changes.
                with contextlib.suppress(OSError):
                    os.fchmod(fd, existing_mode)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())

            os.replace(temp_path, target)
            self._sync_directory(target.parent)
        except OSError as exc:
            raise ConfigIOError(f"Unable to atomically write {self.config_path}: {exc}") from exc
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp_path)

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        """Best-effort directory fsync to persist rename metadata."""
        if os.name == "nt":
            return

        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    @contextlib.contextmanager
    def _file_lock(self, *, exclusive: bool) -> Iterator[None]:
        if exclusive:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_handle = self.lock_path.open("a+b")
        else:
            lock_handle = self._open_lock_for_reading()
        if lock_handle is None:
            # No usable lock file: saves replace the file atomically, so an
            # unlocked reader still sees either the old or the new contents.
            yield
            return

        with lock_handle:
            self._acquire_lock(lock_handle, exclusive=exclusive)
            try:
                yield
            finally:
                self._release_lock(lock_handle)

    def _open_lock_for_reading(self) -> BinaryIO | None:
        """Share an existing lock file that this account may read but not write."""
        if os.name == "nt":
            return None
        try:
            return self.lock_path.open("rb")
        except OSError:
            return None

    @staticmethod
    def _acquire_lock(lock_handle: BinaryIO, *, exclusive: bool) -> None:
        if os.name == "nt":
            import msvcrt

            lock_handle.seek(0, os.SEEK_END)
            if lock_handle.tell() == 0:
                lock_handle.write(b"\0")
                lock_handle.flush()
            lock_handle.seek(0)

            mode = msvcrt.LK_LOCK if exclusive else msvcrt.LK_RLCK
            while True:
                try:
                    msvcrt.locking(lock_handle.fileno(), mode, 1)
                    break
                except OSError:
                    time.sleep(0.05)
            return

        import fcntl

        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(lock_handle.fileno(), mode)

    @staticmethod
    def _release_lock(lock_handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            lock_handle.seek(0)
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
