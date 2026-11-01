"""Installed defaults and path rules do not depend on a source checkout."""

from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from checksi.config import ConfigIOError, ConfigManager, resolve_config_paths, user_config_path
from checksi import main


class PortableConfigurationTests(unittest.TestCase):
    """Factory settings are immutable and file paths have an explicit base."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.user_path = self.root / "user" / "settings.toml"

    def test_missing_user_settings_loads_packaged_factory_without_writing(self) -> None:
        with patch("checksi.config.user_config_path", return_value=self.user_path):
            manager = ConfigManager()
            config = manager.load()
        self.assertEqual(config.paths.input_path, Path("."))
        self.assertEqual(config.paths.output_dir, Path("checksi-output"))
        self.assertEqual(config.conversion.timeout_seconds, 60)
        self.assertFalse(self.user_path.parent.exists())

    def test_explicit_missing_settings_does_not_fall_back(self) -> None:
        with self.assertRaises(ConfigIOError):
            ConfigManager(self.user_path).load()
        self.assertFalse(self.user_path.parent.exists())

    def test_update_missing_explicit_settings_leaves_no_filesystem_artifacts(self) -> None:
        for parent_exists in (False, True):
            with self.subTest(parent_exists=parent_exists):
                path = self.root / str(parent_exists) / "nested" / "settings.toml"
                if parent_exists:
                    path.parent.mkdir(parents=True)
                before = set(self.root.rglob("*"))
                changes = []
                with self.assertRaisesRegex(ConfigIOError, "Configuration file not found"):
                    ConfigManager(path).update(changes.append)
                self.assertEqual(set(self.root.rglob("*")), before)
                self.assertEqual(changes, [])

    def test_file_relative_paths_resolve_against_file_parent_without_mutating_config(self) -> None:
        manager = ConfigManager(self.user_path)
        manager.save(ConfigManager.load_factory())
        config = manager.load()
        before = {p.name: p.read_bytes() for p in self.user_path.parent.iterdir()}
        runtime = manager.resolve_paths(config)
        self.assertEqual(runtime.paths.input_path, self.user_path.parent)
        self.assertEqual(runtime.paths.output_dir, self.user_path.parent / "checksi-output")
        self.assertEqual(config.paths.output_dir, Path("checksi-output"))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.user_path.parent.iterdir()})

    def test_load_never_creates_a_lock_file(self) -> None:
        manager = ConfigManager(self.user_path)
        manager.save(ConfigManager.load_factory())
        manager.lock_path.unlink()
        manager.load()
        self.assertFalse(manager.lock_path.exists())

    def test_factory_runtime_paths_use_invocation_directory(self) -> None:
        with patch("checksi.config.user_config_path", return_value=self.user_path), \
             patch("checksi.config.Path.cwd", return_value=self.root):
            manager = ConfigManager()
            runtime = manager.resolve_paths(manager.load())
        self.assertEqual(runtime.paths.input_path, self.root)
        self.assertEqual(runtime.paths.output_dir, self.root / "checksi-output")

    def test_conversion_relative_executable_uses_same_file_base(self) -> None:
        manager = ConfigManager(self.user_path)
        config = ConfigManager.load_factory()
        config.conversion.libreoffice_path = "tools/soffice"
        manager.save(config)
        runtime = manager.resolve_paths(manager.load())
        self.assertEqual(runtime.conversion.libreoffice_path, str(self.user_path.parent / "tools/soffice"))

    def test_cli_overrides_resolve_against_invocation_instead_of_file(self) -> None:
        manager = ConfigManager(self.user_path)
        manager.save(ConfigManager.load_factory())
        source = manager.resolve_paths(manager.load())
        args = main._build_parser().parse_args([
            "run", "sample.pdf", "--override", "paths.output_dir=outputs",
            "--override", "conversion.libreoffice_path=tools/soffice",
        ])
        runtime = main._build_run_config(source, args)
        self.assertEqual(runtime.paths.input_path, Path.cwd() / "sample.pdf")
        self.assertEqual(runtime.paths.output_dir, Path.cwd() / "outputs")
        self.assertEqual(runtime.conversion.libreoffice_path, str(Path.cwd() / "tools/soffice"))

    def test_first_config_set_preserves_factory_paths_for_unrelated_settings(self):
        output = []
        with patch('checksi.config.user_config_path', return_value=self.user_path), \
             patch('checksi.config.Path.cwd', return_value=self.root):
            manager = ConfigManager()
            before = manager.resolve_paths(manager.load()).paths
            args = main._build_parser().parse_args(['config', 'set', 'modes.output_mode', 'detailed'])
            self.assertEqual(main._handle_config_set(args, manager, log_callback=output.append), 0)
            after = manager.resolve_paths(manager.load()).paths
        self.assertEqual(before, after)
        self.assertFalse(before.output_dir.exists())
        self.assertIn(f"input_path: {before.input_path}", output)
        self.assertIn(f"output_dir: {before.output_dir}", output)
        other_directory = self.root / "other-cwd"
        other_directory.mkdir()
        with patch('checksi.config.Path.cwd', return_value=other_directory):
            from_other_cwd = manager.resolve_paths(manager.load()).paths
        self.assertEqual(from_other_cwd, before)

    def test_stale_cli_clients_update_latest_settings_instead_of_old_snapshots(self):
        ConfigManager(self.user_path).save(ConfigManager.load_factory())
        first, second = ConfigManager(self.user_path), ConfigManager(self.user_path)
        first.load()
        second.load()
        for manager, key, value in ((first, "dp5.timeout_seconds", "900"),
                                    (second, "modes.output_mode", "detailed")):
            args = main._build_parser().parse_args(["config", "set", key, value])
            self.assertEqual(main._handle_config_set(args, manager, log_callback=lambda _: None), 0)
        saved = ConfigManager(self.user_path).load()
        self.assertEqual(saved.dp5.timeout_seconds, 900)
        self.assertEqual(saved.modes.output_mode, "detailed")

    def test_concurrent_cli_updates_serialize_read_modify_write(self):
        ConfigManager(self.user_path).save(ConfigManager.load_factory())
        first_mutating = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        second_mutating = threading.Event()
        results = []
        errors = []
        original = main._set_config_value

        def set_value(config, key, value):
            if key == "dp5.timeout_seconds":
                first_mutating.set()
                if not release_first.wait(5):
                    raise RuntimeError("First update was not released")
            else:
                second_mutating.set()
            original(config, key, value)

        def update(key, value, started=None):
            try:
                if started is not None:
                    started.set()
                args = main._build_parser().parse_args(["config", "set", key, value])
                results.append(main._handle_config_set(args, ConfigManager(self.user_path), log_callback=lambda _: None))
            except Exception as exc:
                errors.append(exc)

        first = threading.Thread(target=update, args=("dp5.timeout_seconds", "900"))
        second = threading.Thread(target=update, args=("modes.output_mode", "detailed", second_started))
        with patch.object(main, "_set_config_value", set_value):
            try:
                first.start()
                self.assertTrue(first_mutating.wait(5))
                second.start()
                self.assertTrue(second_started.wait(5))
                self.assertFalse(second_mutating.wait(0.1))
            finally:
                release_first.set()
                first.join(5)
                if second.ident is not None:
                    second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [0, 0])
        saved = ConfigManager(self.user_path).load()
        self.assertEqual(saved.dp5.timeout_seconds, 900)
        self.assertEqual(saved.modes.output_mode, "detailed")

    def test_independent_cli_process_updates_preserve_all_changes(self):
        ConfigManager(self.user_path).save(ConfigManager.load_factory())
        script = "from checksi.main import main_cli; import sys; raise SystemExit(main_cli(sys.argv[1:]))"
        updates = (("dp5.timeout_seconds", "900"), ("modes.output_mode", "detailed"),
                   ("conversion.timeout_seconds", "120"), ("modes.header_margin", "0.2"))
        processes = [subprocess.Popen([sys.executable, "-c", script, "--config-file", str(self.user_path),
                       "config", "set", key, value], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for key, value in updates]
        try:
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stdout + stderr)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
        saved = ConfigManager(self.user_path).load()
        self.assertEqual(saved.dp5.timeout_seconds, 900)
        self.assertEqual(saved.modes.output_mode, "detailed")
        self.assertEqual(saved.conversion.timeout_seconds, 120)
        self.assertEqual(saved.modes.header_margin, 0.2)

    def test_config_set_load_failures_use_stderr_and_leave_stdout_empty(self):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO

        for name, content in (("missing", None), ("malformed", "invalid TOML")):
            with self.subTest(source=name):
                path = self.root / name / "settings.toml"
                if content is not None:
                    path.parent.mkdir()
                    path.write_text(content, encoding="utf-8")
                stdout, stderr = StringIO(), StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    result = main.main_cli(["--config-file", str(path), "config", "set", "modes.output_mode", "detailed"])
                self.assertEqual(result, 1)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("Error:", stderr.getvalue())
                if content is None:
                    self.assertFalse(path.exists())
                    self.assertFalse(path.parent.exists())
                else:
                    self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_config_set_invalid_key_or_value_keeps_existing_stdout_diagnostics(self):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO

        ConfigManager(self.user_path).save(ConfigManager.load_factory())
        before = self.user_path.read_bytes()
        for key, value in (("modes.unknown", "detailed"), ("dp5.timeout_seconds", "0")):
            with self.subTest(key=key):
                stdout, stderr = StringIO(), StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    result = main.main_cli(["--config-file", str(self.user_path), "config", "set", key, value])
                self.assertEqual(result, 1)
                self.assertIn("Error:", stdout.getvalue())
                self.assertEqual(stderr.getvalue(), "")
                self.assertEqual(self.user_path.read_bytes(), before)

    def test_rejected_factory_config_set_creates_no_directories_or_locks(self):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO

        for key, value in (
            ("modes.unknown", "x"),
            ("dp5.timeout_seconds", "0"),
            ("paths.output_dir", "~no_such_user_for_checksi/out"),
        ):
            for parent_exists in (False, True):
                with self.subTest(key=key, parent_exists=parent_exists):
                    path = self.root / key / str(parent_exists) / "settings.toml"
                    if parent_exists:
                        path.parent.mkdir(parents=True)
                    before = set(self.root.rglob("*"))
                    stdout, stderr = StringIO(), StringIO()
                    with patch("checksi.config.user_config_path", return_value=path), \
                         redirect_stdout(stdout), redirect_stderr(stderr):
                        code = main.main_cli(["config", "set", key, value])
                    self.assertEqual(code, 1)
                    self.assertIn("Error:", stdout.getvalue())
                    self.assertEqual(stderr.getvalue(), "")
                    self.assertEqual(set(self.root.rglob("*")), before)

    def test_factory_prevalidation_does_not_replace_a_concurrently_created_file(self):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO

        original = ConfigManager.update

        def create_before_update(manager, change):
            latest = ConfigManager.load_factory()
            latest.dp5.timeout_seconds = 903
            ConfigManager(self.user_path).save(latest)
            return original(manager, change)

        stdout, stderr = StringIO(), StringIO()
        with patch("checksi.config.user_config_path", return_value=self.user_path), \
             patch.object(ConfigManager, "update", create_before_update), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            code = main.main_cli(["config", "set", "dp5.probability_threshold_percent", "12"])
        self.assertEqual(code, 0)
        saved = ConfigManager(self.user_path).load()
        self.assertEqual(saved.dp5.timeout_seconds, 903)
        self.assertEqual(saved.dp5.score_threshold_percent, 12)
        self.assertEqual(stderr.getvalue().count("Configuration notice:"), 1)
        self.assertNotIn("Materialized", stdout.getvalue())

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "POSIX permissions as non-root")
    def test_read_only_config_set_preserves_write_error_channel_and_load_notices(self):
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO
        from tests.test_dp5q_config import CONFIG

        self.user_path.parent.mkdir()
        self.user_path.write_text(CONFIG + "probability_threshold_percent = 17.5\nmanaged = true\n")
        original = self.user_path.read_bytes()
        os.chmod(self.user_path.parent, 0o555)
        self.addCleanup(os.chmod, self.user_path.parent, 0o755)
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main.main_cli(["--config-file", str(self.user_path), "config", "set", "modes.output_mode", "detailed"])
        self.assertEqual(code, 1)
        self.assertIn("Unable to write configuration file", stdout.getvalue())
        self.assertNotIn("Error:", stderr.getvalue())
        self.assertEqual(stderr.getvalue().count("Configuration notice:"), 3)
        self.assertIn('nmr_configuration_dependent', stderr.getvalue())
        self.assertEqual(self.user_path.read_bytes(), original)
        self.assertFalse(ConfigManager(self.user_path).lock_path.exists())

    def test_factory_view_bypasses_selected_settings_without_writing(self):
        from io import StringIO
        from contextlib import redirect_stdout
        import json
        output = StringIO()
        with redirect_stdout(output):
            result = main.main_cli(['--config-file', str(self.user_path), 'config', 'view', '--factory'])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())['paths']['output_dir'], 'checksi-output')
        self.assertFalse(self.user_path.parent.exists())

    def test_entrypoint_emits_unicode_paths_as_utf8_with_non_utf8_initial_streams(self):
        import os
        import subprocess
        import sys
        path = self.root / 'settings café α' / 'settings.toml'
        ConfigManager(path).save(ConfigManager.load_factory())
        arguments = ['checksi', '--config-file', str(path), 'config', 'set', 'modes.output_mode', 'detailed']
        script = f"import sys; sys.argv={arguments!r}; from checksi.main import entrypoint; raise SystemExit(entrypoint())"
        environment = dict(os.environ, PYTHONIOENCODING='ascii:strict', PYTHONUTF8='0')
        completed = subprocess.run([sys.executable, '-c', script], env=environment,
            capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn(str(path.resolve()), completed.stdout)
        self.assertEqual(ConfigManager(path).load().modes.output_mode, 'detailed')

    def test_configuration_cli_does_not_load_document_or_chemistry_libraries(self):
        import subprocess
        import sys
        script = """import sys
from checksi.main import main_cli
assert main_cli(['config', 'view', '--factory']) == 0
for name in ('rdkit', 'fitz', 'pymupdf', 'openpyxl', 'numpy', 'scipy', 'PySide6'):
    assert name not in sys.modules, name
"""
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_conversion_limits_reject_unbounded_timeout(self) -> None:
        from pydantic import ValidationError
        config = ConfigManager.load_factory()
        for timeout in (0, 3601):
            with self.subTest(timeout=timeout), self.assertRaises(ValidationError):
                config.conversion.timeout_seconds = timeout

    def test_blank_converter_values_clear_the_cli_and_schema_setting(self) -> None:
        config = ConfigManager.load_factory()
        for blank in ("", " ", "\t\n"):
            with self.subTest(blank=blank):
                config.conversion.libreoffice_path = blank
                self.assertIsNone(config.conversion.libreoffice_path)
        manager = ConfigManager(self.user_path)
        config.conversion.libreoffice_path = "soffice"
        manager.save(config)
        output = []
        args = main._build_parser().parse_args([
            "config", "set", "conversion.libreoffice_path", "",
        ])
        self.assertEqual(main._handle_config_set(args, manager, log_callback=output.append), 0)
        self.assertIsNone(manager.load().conversion.libreoffice_path)
        self.assertNotIn("libreoffice_path", self.user_path.read_text())

    def test_bare_converter_commands_remain_path_search_names(self) -> None:
        config = ConfigManager.load_factory()
        for command in ("soffice", "soffice.exe"):
            with self.subTest(command=command):
                config.conversion.libreoffice_path = command
                self.assertEqual(resolve_config_paths(config, self.root).conversion.libreoffice_path, command)
        for raw_path in ("./soffice", "tools/soffice"):
            with self.subTest(raw_path=raw_path):
                config.conversion.libreoffice_path = raw_path
                self.assertEqual(resolve_config_paths(config, self.root).conversion.libreoffice_path,
                                 str(self.root / raw_path))

    def test_runtime_input_keeps_symlink_filename_and_suffix(self) -> None:
        target = self.root / "blob-0001"
        target.touch()
        logical_input = self.root / "article.pdf"
        try:
            logical_input.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Symlinks are unavailable: {exc}")
        config = ConfigManager.load_factory()
        config.paths.input_path = logical_input
        self.assertEqual(resolve_config_paths(config).paths.input_path, logical_input)
        args = main._build_parser().parse_args(["run", str(logical_input)])
        self.assertEqual(main._build_run_config(config, args).paths.input_path, logical_input)

    def test_user_locations_are_absolute_and_reject_relative_environment_bases(self) -> None:
        home = self.root / "home"
        absolute_base = self.root / "custom"
        cases = [
            ("win32", {}, home / "AppData/Local/CheckSI/settings.toml"),
            ("win32", {"LOCALAPPDATA": ""}, home / "AppData/Local/CheckSI/settings.toml"),
            ("win32", {"LOCALAPPDATA": "relative"}, home / "AppData/Local/CheckSI/settings.toml"),
            ("win32", {"LOCALAPPDATA": str(absolute_base)}, absolute_base / "CheckSI/settings.toml"),
            ("darwin", {}, home / "Library/Application Support/CheckSI/settings.toml"),
            ("linux", {}, home / ".config/checksi/settings.toml"),
            ("linux", {"XDG_CONFIG_HOME": ""}, home / ".config/checksi/settings.toml"),
            ("linux", {"XDG_CONFIG_HOME": "relative"}, home / ".config/checksi/settings.toml"),
            ("linux", {"XDG_CONFIG_HOME": str(absolute_base)}, absolute_base / "checksi/settings.toml"),
        ]
        for platform, environment, expected in cases:
            with self.subTest(platform=platform, environment=environment), \
                 patch("checksi.config.sys.platform", platform), \
                 patch("checksi.config.Path.home", return_value=home), \
                 patch.dict(os.environ, environment, clear=True):
                actual = user_config_path()
                self.assertEqual(actual, expected)
                self.assertTrue(actual.is_absolute())

    def test_absolute_environment_location_does_not_require_home(self) -> None:
        for platform, key in (("win32", "LOCALAPPDATA"), ("linux", "XDG_CONFIG_HOME")):
            with self.subTest(platform=platform), \
                 patch("checksi.config.sys.platform", platform), \
                 patch("checksi.config.Path.home", side_effect=RuntimeError("no home")), \
                 patch.dict(os.environ, {key: str(self.root)}, clear=True):
                self.assertTrue(user_config_path().is_absolute())

    def test_explicit_and_factory_cli_work_without_a_home_and_default_errors_are_clean(self) -> None:
        ConfigManager(self.user_path).save(ConfigManager.load_factory())
        cases = [
            (["--config-file", str(self.user_path), "config", "view"], 0),
            (["config", "view", "--factory"], 0),
            (["config", "view"], 1),
        ]
        for arguments, expected in cases:
            script = f"""import os
from unittest.mock import patch
with patch.dict(os.environ, {{}}, clear=True), patch('pathlib.Path.home', side_effect=RuntimeError('no home')):
    from checksi.main import main_cli
    assert main_cli({arguments!r}) == {expected}
"""
            with self.subTest(arguments=arguments):
                completed = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                           text=True, timeout=30)
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                self.assertNotIn("Traceback", completed.stderr)
                if expected:
                    self.assertIn("Error:", completed.stderr)

    def test_factory_resource_failure_has_a_clean_cli_error(self) -> None:
        from contextlib import redirect_stderr
        from io import StringIO
        errors = StringIO()
        with patch("checksi.config.resources.files", side_effect=FileNotFoundError("missing defaults")), \
             redirect_stderr(errors):
            self.assertEqual(main.main_cli(["config", "view", "--factory"]), 1)
        self.assertIn("Unable to load packaged default settings", errors.getvalue())

    def test_missing_gui_dependency_hint_uses_the_trusted_distribution_and_checkout(self) -> None:
        script = """import importlib.abc
import sys
class HideQt(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'PySide6':
            raise ModuleNotFoundError("No module named 'PySide6'", name='PySide6')
sys.meta_path.insert(0, HideQt())
from checksi.gui_main import main_gui
assert main_gui([]) == 1
"""
        completed = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                   text=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("uv tool install --force --python 3.13 --constraints", completed.stdout)
        self.assertIn("checksi-0.4.0-py3-none-any.whl[gui]", completed.stdout)
        self.assertIn("uv sync --locked --python 3.13 --extra gui", completed.stdout)
        self.assertNotIn("pip install", completed.stdout)


if __name__ == "__main__":
    unittest.main()
