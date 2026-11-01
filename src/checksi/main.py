"""Command-line entry point for CheckSI."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import sys
import threading
import time
import tomllib
from enum import IntEnum
from pathlib import Path
from typing import Annotated, Any, get_args, get_origin

from pydantic import BaseModel, TypeAdapter, ValidationError

from checksi.config import ConfigError, ConfigManager, resolve_config_paths
from checksi.config_schema import AppConfig, DeepNameAnalysis, OutputMode
from checksi.dp5q.client import DP5qAvailabilityError as DP5RuntimeValidationError, doctor_runtime
from checksi.events import ApplicationEvent, LogEvent, ProgressEvent

class ExitCode(IntEnum):
    """Semantic process exit codes for automation pipelines."""

    SUCCESS = 0
    SYSTEM_ERROR = 1
    ANALYSIS_ISSUES_FOUND = 2
    PARTIAL_RESULT = 3
    CANCELLED = 130


class ConfigPathError(ValueError):
    """Raised when a dotted config path cannot be resolved or validated."""


class CLIUsageError(ValueError):
    """Raised for command-line argument validation failures."""


class CheckSIArgumentParser(argparse.ArgumentParser):
    """Argument parser that reports usage errors without exiting with code 2."""

    def error(self, message: str) -> None:  # noqa: D401
        raise CLIUsageError(message)


def _parse_cli_literal(raw_value: str) -> Any:
    """Parse TOML-like literals while still allowing bare strings."""
    try:
        return tomllib.loads(f"value = {raw_value}")["value"]
    except tomllib.TOMLDecodeError:
        return raw_value


def _strip_annotated(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin is None:
        return annotation

    # typing.Annotated[T, ...] -> T
    if origin is Annotated:
        return get_args(annotation)[0]
    return annotation


def _parse_index(token: str, *, container_name: str) -> int:
    try:
        return int(token)
    except ValueError as exc:
        raise ConfigPathError(f"{container_name} expects a numeric index, got '{token}'.") from exc


def _validate_with_type(annotation: Any, raw_value: str) -> Any:
    parsed_input = _parse_cli_literal(raw_value)
    normalized_annotation = _strip_annotated(annotation)
    try:
        return TypeAdapter(normalized_annotation).validate_python(parsed_input)
    except ValidationError as exc:
        raise ConfigPathError(str(exc)) from exc


def _dict_types(annotation: Any) -> tuple[Any, Any]:
    normalized = _strip_annotated(annotation)
    origin = get_origin(normalized)
    if origin is not dict:
        return str, Any
    args = get_args(normalized)
    if len(args) != 2:
        return str, Any
    return args[0], args[1]


def _list_element_type(annotation: Any) -> Any:
    normalized = _strip_annotated(annotation)
    origin = get_origin(normalized)
    if origin is not list:
        return Any
    args = get_args(normalized)
    if not args:
        return Any
    return args[0]


def _resolve_parent(container: Any, annotation: Any, tokens: list[str]) -> tuple[Any, Any, str]:
    if not tokens:
        raise ConfigPathError("Configuration path is empty.")

    current_value = container
    current_annotation = annotation

    for token in tokens[:-1]:
        if isinstance(current_value, BaseModel):
            field = type(current_value).model_fields.get(token)
            if field is None:
                raise ConfigPathError(
                    f"Unknown config key '{token}' on {current_value.__class__.__name__}."
                )
            current_value = getattr(current_value, token)
            current_annotation = field.annotation
            continue

        if isinstance(current_value, list):
            index = _parse_index(token, container_name="List path")
            if index < 0 or index >= len(current_value):
                raise ConfigPathError(f"List index out of range: {index}.")
            current_value = current_value[index]
            current_annotation = _list_element_type(current_annotation)
            continue

        if isinstance(current_value, dict):
            key_type, value_type = _dict_types(current_annotation)
            typed_key = _validate_with_type(key_type, token)
            if typed_key not in current_value:
                raise ConfigPathError(f"Dictionary key not found: '{token}'.")
            current_value = current_value[typed_key]
            current_annotation = value_type
            continue

        raise ConfigPathError(f"Cannot traverse into '{token}' from {type(current_value).__name__}.")

    return current_value, current_annotation, tokens[-1]


def _set_config_value(cfg: AppConfig, dotted_path: str, raw_value: str) -> None:
    dotted_path = _canonical_config_path(dotted_path)
    tokens = [token for token in dotted_path.split(".") if token]
    parent, parent_annotation, leaf_token = _resolve_parent(cfg, AppConfig, tokens)

    if isinstance(parent, BaseModel):
        field = type(parent).model_fields.get(leaf_token)
        if field is None:
            raise ConfigPathError(
                f"Unknown config key '{leaf_token}' on {parent.__class__.__name__}."
            )
        typed_value = _validate_with_type(field.annotation, raw_value)
        setattr(parent, leaf_token, typed_value)
        return

    if isinstance(parent, list):
        index = _parse_index(leaf_token, container_name="List path")
        if index < 0 or index >= len(parent):
            raise ConfigPathError(f"List index out of range: {index}.")
        element_type = _list_element_type(parent_annotation)
        parent[index] = _validate_with_type(element_type, raw_value)
        return

    if isinstance(parent, dict):
        key_type, value_type = _dict_types(parent_annotation)
        typed_key = _validate_with_type(key_type, leaf_token)
        parent[typed_key] = _validate_with_type(value_type, raw_value)
        return

    raise ConfigPathError(f"Cannot assign into {type(parent).__name__}.")


def _canonical_config_path(dotted_path: str) -> str:
    """Resolve the load-compatible threshold spelling before schema lookup."""
    tokens = [token for token in dotted_path.split(".") if token]
    if tokens == ["dp5", "probability_threshold_percent"]:
        print("Configuration notice: dp5.probability_threshold_percent is deprecated; "
              "use dp5.score_threshold_percent.", file=sys.stderr)
        return "dp5.score_threshold_percent"
    return ".".join(tokens)


def _normalized_overrides(overrides: list[str]) -> list[tuple[str, str]]:
    """Normalize one incoming CLI layer without hiding spelling conflicts."""
    parsed = [
        (".".join(token for token in key.split(".") if token), value)
        for key, value in (_parse_override(override) for override in overrides)
    ]
    thresholds = {
        key: value for key, value in parsed
        if key in {"dp5.probability_threshold_percent", "dp5.score_threshold_percent"}
    }
    if len(thresholds) == 2:
        old = _validate_with_type(float, thresholds["dp5.probability_threshold_percent"])
        new = _validate_with_type(float, thresholds["dp5.score_threshold_percent"])
        if old != new:
            raise ConfigPathError("Conflicting DP5 threshold override spellings in the same command.")
    return [(_canonical_config_path(key), value) for key, value in parsed]


def _config_path_from_args(args) -> Path | None:
    return Path(args.config_file).expanduser() if args.config_file else None


def _write_advisory_stderr(message: str) -> bool:
    """Keep progress and configuration notices harmless when their reader closes."""
    try:
        print(message, file=sys.stderr, flush=True)
    except OSError:
        # A buffered broken pipe can otherwise change the exit code to 120
        # when Python flushes stderr at shutdown.
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
        return False
    return True


def _load_config(manager: ConfigManager, *, log_callback) -> AppConfig | None:
    try:
        config = manager.load()
    except ConfigError as exc:
        # Keep stdout for command output, such as the `config view` JSON.
        print(f"\nError: {exc}", file=sys.stderr)
        return None
    for notice in getattr(manager, "migration_notices", ()):
        _write_advisory_stderr(f"Configuration notice: {notice}")
    return config


def _parse_override(override: str) -> tuple[str, str]:
    if "=" not in override:
        raise ConfigPathError(
            f"Invalid override '{override}'. Expected KEY=VALUE (e.g. modes.output_mode=detailed)."
        )
    key, value = override.split("=", 1)
    key = key.strip()
    if not key:
        raise ConfigPathError(f"Invalid override '{override}'. Key cannot be empty.")
    return key, value.strip()


def _build_run_config(base_cfg: AppConfig, args) -> AppConfig:
    cfg = base_cfg.model_copy(deep=True)

    cfg.paths.input_path = Path(args.input_file).expanduser()

    if args.output_dir:
        cfg.paths.output_dir = Path(args.output_dir).expanduser()

    if args.deep_name_analysis:
        cfg.modes.deep_name_analysis = DeepNameAnalysis(args.deep_name_analysis)
    if args.output_mode:
        cfg.modes.output_mode = OutputMode(args.output_mode)
    if getattr(args, "dp5", None) is not None:
        cfg.dp5.enabled = bool(args.dp5)

    for key, value in _normalized_overrides(args.override):
        _set_config_value(cfg, key, value)

    return resolve_config_paths(AppConfig.model_validate(cfg.model_dump()))


def _format_path(path_value: str | None) -> str:
    if not path_value:
        return "not generated"
    return os.path.abspath(os.path.normpath(path_value))


def _build_event_handler(*, log_callback, progress_callback=None):
    _log = log_callback or print
    _progress = progress_callback or (lambda current, total, message="": None)

    def event_handler(event: ApplicationEvent) -> None:
        if isinstance(event, LogEvent):
            _log(event.message)
            return
        if isinstance(event, ProgressEvent):
            _progress(event.current, event.total, event.message)

    return event_handler


def _print_run_summary(result, elapsed_seconds: float, *, log_callback) -> None:
    _log = log_callback or print
    compounds = int(result.analysis_data.get("total_found", 0))
    issues = result.issue_counts

    _log("\n=== Run Summary ===")
    _log(f"input_path: {result.input_path}")
    _log(f"compounds_detected: {compounds}")
    _log(f"critical_issues: {issues['critical']}")
    _log(f"warning_issues: {issues['warning']}")
    _log(f"informational_notes: {issues['info']}")
    assessment = result.analysis_data.get("assessment")
    if assessment is not None:
        _log(f"assessment_status: {assessment['status']}")
        _log(f"assessed_descriptions: {assessment['assessed_descriptions']}")
        _log(f"unassessed_records: {len(assessment['unassessed'])}")
    _log(f"report_html: {_format_path(result.report_data.get('html_path'))}")
    _log(f"report_pdf: {_format_path(result.report_data.get('pdf_path'))}")
    _log(f"results_json: {_format_path(result.report_data.get('analysis_path'))}")
    _log(f"error_stats_xlsx: {_format_path(result.report_data.get('error_stats_path'))}")
    _log(f"source_annotated_pdf: {_format_path(result.report_data.get('source_pdf_path'))}")
    _log(f"output_dir: {_format_path(result.report_data.get('output_dir'))}")
    export_status = "partial" if result.report_data.get("partial") else "complete"
    if result.report_data.get("aborted"):
        export_status = "cancelled"
    _log(f"export_status: {export_status}")
    for warning in result.report_data.get("artifact_warnings", ()):
        _log(f"artifact_warning: {warning}")
    for warning in result.report_data.get("annotation_warnings", ()):
        _log(f"annotation_coverage: {warning}")
    _log(f"elapsed_seconds: {elapsed_seconds:.2f}")


class _CLIProgressSink:
    """Keep advisory stderr output coarse and harmless when its reader closes."""

    def __init__(self, cancel_token: threading.Event) -> None:
        self._cancel_token = cancel_token
        self._enabled = True
        self._cancellation_shown = False
        self._last_step: tuple[int, int] | None = None

    def _write(self, message: str) -> None:
        if not self._enabled:
            return
        self._enabled = _write_advisory_stderr(message)

    def notify_cancellation(self) -> None:
        """Announce cancellation from normal execution, outside the handler."""
        if self._cancel_token.is_set() and not self._cancellation_shown:
            self._cancellation_shown = True
            self._write("Cancellation requested; waiting for the current operation to finish.")

    def __call__(self, current: int, total: int, message: str = "") -> None:
        self.notify_cancellation()
        if not self._enabled:
            return
        try:
            interactive = sys.stderr.isatty()
        except OSError:
            interactive = False
        if total > 0 and not interactive:
            step = (total, min(10, current * 10 // total))
            if self._last_step == step and current != total:
                return
            self._last_step = step
        else:
            self._last_step = None
        detail = message or (f"{current}/{total}" if total > 0 else "Working")
        self._write(f"Progress: {detail}")


def _cancellation_signals() -> tuple[signal.Signals, ...]:
    """Signals that stop a run: Ctrl+C, termination requests and terminal hangup."""
    names = ("SIGINT", "SIGTERM", "SIGHUP")
    return tuple(getattr(signal, name) for name in names if hasattr(signal, name))


@contextlib.contextmanager
def _handle_run_sigint(cancel_token: threading.Event):
    """Temporarily turn main-thread stop signals into cooperative cancellation."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def request_cancellation(signum, frame) -> None:
        cancel_token.set()

    previous = {}
    try:
        for signum in _cancellation_signals():
            handler = signal.getsignal(signum)
            # An inherited ignore (for example from nohup) stays in force.
            if handler == signal.SIG_IGN:
                continue
            previous[signum] = handler
            signal.signal(signum, request_cancellation)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _handle_run(args, manager: ConfigManager, *, log_callback) -> int:
    # Configuration and help must work without loading the native chemistry stack.
    from checksi.application_runner import ApplicationRunner
    from checksi.file_io import FileIOError

    _log = log_callback or print
    loaded = _load_config(manager, log_callback=_log)
    if loaded is None:
        return int(ExitCode.SYSTEM_ERROR)

    try:
        source_config = manager.resolve_paths(loaded)
        run_cfg = _build_run_config(source_config, args)
    except (ConfigPathError, ValidationError, ValueError) as exc:
        _log(f"\nError: {exc}")
        return int(ExitCode.SYSTEM_ERROR)

    _log("\nStarting analysis...")
    cancel_token = threading.Event()
    progress = _CLIProgressSink(cancel_token)
    event_handler = _build_event_handler(log_callback=_log, progress_callback=progress)
    start_time = time.time()
    with _handle_run_sigint(cancel_token):
        try:
            runner = ApplicationRunner(event_dispatcher=event_handler)
            result = runner.execute(run_cfg, str(run_cfg.paths.input_path), cancel_token)
        except (FileIOError, OSError, RuntimeError, ValueError) as exc:
            progress.notify_cancellation()
            _log(f"\nError: {exc}")
            if isinstance(exc, DP5RuntimeValidationError) and not cancel_token.is_set():
                _log("Run `checksi dp5 doctor` to diagnose the bundled DP5q installation.")
            if cancel_token.is_set():
                _log("\nRun cancelled.")
                return int(ExitCode.CANCELLED)
            return int(ExitCode.SYSTEM_ERROR)

    progress.notify_cancellation()
    elapsed = time.time() - start_time
    _print_run_summary(result, elapsed, log_callback=_log)

    if cancel_token.is_set() or result.analysis_data.get("aborted") or result.report_data.get("aborted"):
        _log("\nRun cancelled.")
        return int(ExitCode.CANCELLED)
    if (result.report_data.get("partial")
            or result.analysis_data.get("assessment", {}).get("status") in ("none", "partial")):
        return int(ExitCode.PARTIAL_RESULT)
    if result.issue_counts["anomaly_count"] > 0:
        return int(ExitCode.ANALYSIS_ISSUES_FOUND)
    return int(ExitCode.SUCCESS)


def _handle_config_view(manager: ConfigManager, *, log_callback, factory: bool = False) -> int:
    _log = log_callback or print
    try:
        cfg = manager.load_factory() if factory else _load_config(manager, log_callback=_log)
    except ConfigError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return int(ExitCode.SYSTEM_ERROR)
    if cfg is None:
        return int(ExitCode.SYSTEM_ERROR)

    rendered = json.dumps(cfg.model_dump(mode="json"), indent=2, sort_keys=True)
    _log(rendered)
    return int(ExitCode.SUCCESS)


def _handle_config_set(args, manager: ConfigManager, *, log_callback) -> int:
    _log = log_callback or print
    preflight = _load_config(manager, log_callback=_log)
    if preflight is None:
        return int(ExitCode.SYSTEM_ERROR)
    shown_notices = set(manager.migration_notices)
    materializing_factory = False

    def change(config: AppConfig) -> None:
        nonlocal materializing_factory
        materializing_factory = manager.uses_factory_defaults
        for notice in manager.migration_notices:
            if notice not in shown_notices:
                _write_advisory_stderr(f"Configuration notice: {notice}")
        _set_config_value(config, key, args.value)

    try:
        key = _canonical_config_path(args.key)
        if manager.uses_factory_defaults:
            candidate = preflight.model_copy(deep=True)
            _set_config_value(candidate, key, args.value)
            manager.resolve_paths(AppConfig.model_validate(candidate.model_dump()))
        saved = manager.update(change)
    except (ConfigPathError, ValidationError, ConfigError, TypeError, ValueError) as exc:
        _log(f"\nError: {exc}")
        return int(ExitCode.SYSTEM_ERROR)
    pinned_config = saved if materializing_factory else None

    _log(f"Updated '{args.key}' in {manager.config_path}")
    if pinned_config is not None:
        _log("Materialized user settings with pinned runtime paths:")
        _log(f"input_path: {pinned_config.paths.input_path}")
        _log(f"output_dir: {pinned_config.paths.output_dir}")
        if pinned_config.conversion.libreoffice_path is not None:
            _log(f"libreoffice_path: {pinned_config.conversion.libreoffice_path}")
    return int(ExitCode.SUCCESS)


def _handle_dp5_doctor(manager: ConfigManager, *, log_callback) -> int:
    _log = log_callback or print
    cfg = _load_config(manager, log_callback=_log)
    if cfg is None:
        return int(ExitCode.SYSTEM_ERROR)

    ok, messages = doctor_runtime(cfg.dp5)
    for message in messages:
        _log(message)

    if not ok:
        _log("Repair or reinstall the CheckSI package to restore its bundled DP5q assets.")
        return int(ExitCode.SYSTEM_ERROR)
    return int(ExitCode.SUCCESS)


def _add_run_arguments(parser: argparse.ArgumentParser, *, optional_input: bool = False) -> None:
    """Share the run/session override interface between the CLI and GUI."""
    input_options = {"nargs": "?"} if optional_input else {}
    parser.add_argument("input_file", help="Path to the input PDF or DOCX file.", **input_options)
    parser.add_argument(
        "--output-dir",
        help="Parent directory for a new, unique run output directory.",
    )
    parser.add_argument(
        "--deep-name-analysis",
        choices=[mode.value for mode in DeepNameAnalysis],
        help="Turn local OPSIN name-to-structure analysis on or off for this run.",
    )
    parser.add_argument(
        "--output-mode",
        choices=[mode.value for mode in OutputMode],
        help="Temporary run-only override for modes.output_mode.",
    )
    dp5_group = parser.add_mutually_exclusive_group()
    dp5_group.add_argument(
        "--dp5",
        dest="dp5",
        action="store_true",
        default=None,
        help="Run optional bundled DP5q compatibility scoring for eligible compounds.",
    )
    dp5_group.add_argument(
        "--no-dp5",
        dest="dp5",
        action="store_false",
        help="Disable DP5q compatibility scoring for this run.",
    )
    parser.add_argument(
        "--override",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "Run-only override without writing config. Example: "
            "--override modes.open_reports_on_success=true"
        ),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = CheckSIArgumentParser(
        description="Check chemical experimental data from PDF and DOCX files.",
    )
    parser.add_argument(
        "--config-file",
        help="Settings TOML file; otherwise use per-user settings or packaged defaults.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="Execute the analysis pipeline for a document.",
    )
    _add_run_arguments(run_parser)

    config_parser = subparsers.add_parser(
        "config",
        help="View or edit persistent configuration values.",
    )
    config_subparsers = config_parser.add_subparsers(dest="config_command", required=True)

    view_parser = config_subparsers.add_parser(
        "view",
        help="Print the currently loaded configuration.",
    )
    view_parser.add_argument("--factory", action="store_true", help="Read packaged factory defaults without user settings.")

    set_parser = config_subparsers.add_parser(
        "set",
        help="Set a persistent config value by dotted path.",
    )
    set_parser.add_argument("key", help="Dotted path (e.g. modes.open_reports_on_success).")
    set_parser.add_argument("value", help="Value (TOML literal or bare string).")

    dp5_parser = subparsers.add_parser(
        "dp5",
        help="Inspect the bundled DP5q installation.",
    )
    dp5_subparsers = dp5_parser.add_subparsers(dest="dp5_command", required=True)
    dp5_subparsers.add_parser(
        "doctor",
        help="Load bundled DP5q assets and run the full model self-check.",
    )

    return parser


def main_cli(argv: list[str] | None = None, *, exit_on_interrupt: bool = False) -> int:
    """Run one CLI command; ``exit_on_interrupt`` ends the process by SIGINT after Ctrl+C."""
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except CLIUsageError as exc:
        print(f"\nError: {exc}")
        parser.print_help()
        return int(ExitCode.SYSTEM_ERROR)

    try:
        manager = ConfigManager(_config_path_from_args(args))
        return _dispatch_command(args, manager, log_callback=print)
    except ConfigError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return int(ExitCode.SYSTEM_ERROR)
    except KeyboardInterrupt:
        # Runs handle Ctrl+C cooperatively; other commands simply stop.
        print("\nCancelled.", file=sys.stderr, flush=True)
        if exit_on_interrupt:
            _exit_by_interrupt()
        return int(ExitCode.CANCELLED)


def _exit_by_interrupt() -> None:
    """End by SIGINT on POSIX so a calling shell also stops, e.g. its loop."""
    if os.name != "posix":
        return
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGINT)


def entrypoint() -> int:
    """Use UTF-8 text pipes and native interrupt semantics on every platform."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")
    return main_cli(exit_on_interrupt=True)


def _dispatch_command(args, manager: ConfigManager, *, log_callback) -> int:
    _log = log_callback

    if args.command == "run":
        return _handle_run(args, manager, log_callback=_log)

    if args.command == "config":
        if args.config_command == "view":
            return _handle_config_view(manager, log_callback=_log, factory=args.factory)
        if args.config_command == "set":
            return _handle_config_set(args, manager, log_callback=_log)

    if args.command == "dp5":
        if args.dp5_command == "doctor":
            return _handle_dp5_doctor(manager, log_callback=_log)

    _log("\nError: Unknown command.")
    return int(ExitCode.SYSTEM_ERROR)


if __name__ == "__main__":
    sys.exit(entrypoint())
