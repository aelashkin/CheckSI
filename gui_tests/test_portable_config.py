"""Desktop session configuration follows the installed CLI path rules."""

from __future__ import annotations

import os
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox

from checksi.config import ConfigManager, resolve_config_paths
from checksi.gui.config_editor import ConfigEditorPage
from checksi.gui.main_window import CheckSIMainWindow
from checksi.gui.theme import ThemeController, ThemePreference
from checksi.gui_main import parse_gui_arguments
from checksi import main
from checksi.main import _build_run_config


class PortableGuiConfigTests(unittest.TestCase):
    """Factory reset is in memory; explicit saves preserve runtime path meaning."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.editor = ConfigEditorPage()
        self.editor.load_from_config(ConfigManager.load_factory())
        self.addCleanup(self.editor.close)

    def test_cli_and_gui_session_arguments_use_identical_override_layer(self) -> None:
        args = parse_gui_arguments(["sample.pdf", "--output-dir", "new-reports", "--override",
                                    "conversion.timeout_seconds=25"])
        config = _build_run_config(resolve_config_paths(ConfigManager.load_factory()), args)
        self.editor.load_from_config(config)
        rebuilt = resolve_config_paths(self.editor.build_config())
        self.assertEqual(rebuilt, config)

    def test_save_as_keeps_relative_session_paths_based_on_invocation(self) -> None:
        destination = self.root / "settings.toml"
        self.editor.output_dir_edit.setText("session-reports")
        with patch("checksi.gui.config_editor.QFileDialog.getSaveFileName", return_value=(str(destination), "")):
            self.editor._save_config()
        saved = ConfigManager(destination).load()
        self.assertEqual(saved.paths.output_dir, Path.cwd() / "session-reports")

    def test_user_default_save_writes_only_the_explicit_user_destination(self) -> None:
        destination = self.root / "user" / "settings.toml"
        before = ConfigManager.load_factory().model_dump()
        with patch("checksi.gui.config_editor.user_config_path", return_value=destination), \
             patch("checksi.gui.config_editor.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
            self.editor._save_default_config()
        self.assertTrue(destination.exists())
        self.assertEqual(ConfigManager.load_factory().model_dump(), before)

    def test_file_save_conflict_keeps_session_edits_and_external_change(self) -> None:
        destination = self.root / "shared.toml"
        ConfigManager(destination).save(ConfigManager.load_factory())
        with patch("checksi.gui.config_editor.QFileDialog.getOpenFileName", return_value=(str(destination), "")):
            self.editor._load_config_from_file()
        self.editor.default_output_mode_control.set_value("detailed")
        external = ConfigManager(destination)
        latest = external.load()
        latest.dp5.timeout_seconds = 900
        external.save(latest)
        before = destination.read_bytes()
        with patch("checksi.gui.config_editor.QFileDialog.getSaveFileName", return_value=(str(destination), "")), \
             patch("checksi.gui.config_editor.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
            self.editor._save_config()
        self.assertEqual(destination.read_bytes(), before)
        self.assertEqual(self.editor.build_config().modes.output_mode, "detailed")

    def test_conflict_reload_requires_consent_then_allows_reviewed_save(self) -> None:
        destination = self.root / "shared.toml"
        ConfigManager(destination).save(ConfigManager.load_factory())
        with patch("checksi.gui.config_editor.QFileDialog.getOpenFileName", return_value=(str(destination), "")):
            self.editor._load_config_from_file()
        self.editor.default_output_mode_control.set_value("detailed")
        external = ConfigManager(destination)
        external.update(lambda config: setattr(config.dp5, "timeout_seconds", 900))
        before = destination.read_bytes()
        loaded = []
        self.editor.config_loaded.connect(loaded.append)
        with patch("checksi.gui.config_editor.QFileDialog.getSaveFileName", return_value=(str(destination), "")), \
             patch("checksi.gui.config_editor.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes) as question:
            self.editor._save_config()
            self.assertIn("discard", question.call_args.args[2])
            self.assertEqual(destination.read_bytes(), before)
            self.assertEqual(self.editor.build_config().dp5.timeout_seconds, 900)
            self.assertEqual(self.editor.build_config().modes.output_mode, "essential")
            self.assertEqual(len(loaded), 1)
            self.editor.default_output_mode_control.set_value("detailed")
            self.editor._save_config()
            self.editor.output_dir_edit.setText("reviewed-reports")
            self.editor._save_config()
        saved = ConfigManager(destination).load()
        self.assertEqual(saved.dp5.timeout_seconds, 900)
        self.assertEqual(saved.modes.output_mode, "detailed")
        self.assertEqual(saved.paths.output_dir, Path.cwd() / "reviewed-reports")
        self.assertEqual(question.call_count, 1)

    def test_save_as_existing_destination_requires_consent_and_guards_its_revision(self) -> None:
        destination = self.root / "existing.toml"
        ConfigManager(destination).save(ConfigManager.load_factory())
        before = destination.read_bytes()
        with patch("checksi.gui.config_editor.QFileDialog.getSaveFileName", return_value=(str(destination), "")), \
             patch("checksi.gui.config_editor.QMessageBox.question", return_value=QMessageBox.StandardButton.No):
            self.editor._save_config()
        self.assertEqual(destination.read_bytes(), before)
        responses = []

        def respond(_parent, title, *_args):
            responses.append(title)
            if title == "Replace Configuration":
                destination.write_bytes(b"# Concurrent edit\n" + before)
                return QMessageBox.StandardButton.Yes
            return QMessageBox.StandardButton.No

        with patch("checksi.gui.config_editor.QFileDialog.getSaveFileName", return_value=(str(destination), "")), \
             patch("checksi.gui.config_editor.QMessageBox.question", side_effect=respond):
            self.editor._save_config()
        self.assertEqual(responses, ["Replace Configuration", "Settings Changed"])
        self.assertEqual(destination.read_bytes(), b"# Concurrent edit\n" + before)

    def test_save_as_existing_destination_asks_once_with_real_file_dialog(self) -> None:
        destination = self.root / "existing.toml"
        ConfigManager(destination).save(ConfigManager.load_factory())
        self.editor.default_output_mode_control.set_value("detailed")
        picker_options, confirmations, timed_out = [], [], []

        def drive_dialogs():
            for widget in self.app.topLevelWidgets():
                if not widget.isVisible():
                    continue
                if isinstance(widget, QFileDialog) and not picker_options:
                    picker_options.append(widget.options())
                    widget.selectFile(str(destination))
                    QTimer.singleShot(0, widget.accept)
                elif isinstance(widget, QMessageBox):
                    confirmations.append(widget.text())
                    widget.button(QMessageBox.StandardButton.Yes).click()

        def cancel_dialogs():
            timed_out.append(True)
            for widget in self.app.topLevelWidgets():
                if isinstance(widget, QDialog):
                    widget.reject()

        driver, deadline = QTimer(), QTimer()
        driver.timeout.connect(drive_dialogs)
        deadline.setSingleShot(True)
        deadline.timeout.connect(cancel_dialogs)
        driver.start(0)
        deadline.start(5000)
        try:
            self.editor._save_config()
        finally:
            driver.stop()
            deadline.stop()
        self.assertFalse(timed_out, f"Save As dialogs did not complete: {confirmations}")
        self.assertEqual(len(confirmations), 1, confirmations)
        self.assertIn("Replace the existing settings", confirmations[0])
        self.assertEqual(len(picker_options), 1)
        self.assertTrue(picker_options[0] & QFileDialog.Option.DontUseNativeDialog)
        self.assertTrue(picker_options[0] & QFileDialog.Option.DontConfirmOverwrite)
        self.assertEqual(ConfigManager(destination).load().modes.output_mode, "detailed")

    def test_failed_conflict_reload_keeps_unsaved_edits(self) -> None:
        destination = self.root / "shared.toml"
        ConfigManager(destination).save(ConfigManager.load_factory())
        with patch("checksi.gui.config_editor.QFileDialog.getOpenFileName", return_value=(str(destination), "")):
            self.editor._load_config_from_file()
        self.editor.default_output_mode_control.set_value("detailed")
        invalid = destination.read_text(encoding="utf-8").replace("timeout_seconds = 600", "timeout_seconds = 0")
        destination.write_text(invalid, encoding="utf-8")
        with patch("checksi.gui.config_editor.QFileDialog.getSaveFileName", return_value=(str(destination), "")), \
             patch("checksi.gui.config_editor.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes) as question, \
             patch("checksi.gui.config_editor.QMessageBox.warning") as warning:
            self.editor._save_config()
        self.assertEqual(question.call_args.args[1], "Settings Changed")
        self.assertEqual(destination.read_text(encoding="utf-8"), invalid)
        self.assertEqual(self.editor.build_config().modes.output_mode, "detailed")
        warning.assert_called_once()

    def test_bootstrap_retains_original_revision_through_session_overrides(self) -> None:
        from checksi.gui import app as bootstrap

        for existing in (False, True):
            with self.subTest(existing=existing):
                destination = self.root / str(existing) / "settings.toml"
                if existing:
                    ConfigManager(destination).save(ConfigManager.load_factory())
                application = Mock(wraps=self.app)
                application.exec.return_value = 0
                theme = Mock()
                theme.load_preference.return_value = ThemePreference.SYSTEM
                windows = []

                def create_window(**kwargs):
                    window = CheckSIMainWindow(**kwargs)
                    self.addCleanup(window.close)
                    windows.append(window)
                    return window

                args = ["checksi-gui", "--override", "modes.output_mode=detailed"]
                if existing:
                    args += ["--config-file", str(destination)]
                with patch.object(bootstrap, "QApplication", return_value=application), \
                     patch.object(bootstrap, "ThemeController", return_value=theme), \
                     patch.object(bootstrap, "CheckSIMainWindow", side_effect=create_window), \
                     patch.object(CheckSIMainWindow, "_refresh_dp5_runtime_status"), \
                     patch("checksi.config.user_config_path", return_value=destination):
                    self.assertEqual(bootstrap.main(args), 0)
                external = ConfigManager(destination)
                external.save(ConfigManager.load_factory())
                external.update(lambda config: setattr(config.dp5, "timeout_seconds", 900))
                before = destination.read_bytes()
                editor = windows[0].config_editor_page
                with patch("checksi.gui.config_editor.user_config_path", return_value=destination), \
                     patch("checksi.gui.config_editor.QMessageBox.question", side_effect=[
                         QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No,
                     ]) as question:
                    editor._save_default_config()
                self.assertEqual(destination.read_bytes(), before)
                self.assertEqual(editor.build_config().modes.output_mode, "detailed")
                self.assertEqual(question.call_count, 2)

    def test_save_as_earlier_source_or_defaults_requires_replacement_consent(self) -> None:
        for initial_defaults in (False, True):
            with self.subTest(initial_defaults=initial_defaults):
                self.editor.load_from_config(ConfigManager.load_factory())
                first = self.root / f"first-{initial_defaults}.toml"
                second = self.root / f"second-{initial_defaults}.toml"
                first_config = ConfigManager.load_factory()
                first_config.dp5.timeout_seconds = 111
                ConfigManager(first).save(first_config)
                second_config = first_config.model_copy(deep=True)
                second_config.dp5.timeout_seconds = 999
                ConfigManager(second).save(second_config)
                if initial_defaults:
                    manager = ConfigManager(first)
                    self.editor.load_from_config(manager.resolve_paths(manager.load()))
                    self.editor.set_config_source(manager)
                else:
                    with patch("checksi.gui.config_editor.QFileDialog.getOpenFileName", return_value=(str(first), "")):
                        self.editor._load_config_from_file()
                self.assertTrue(self.editor.confirm_changes())
                with patch("checksi.gui.config_editor.QFileDialog.getOpenFileName", return_value=(str(second), "")):
                    self.editor._load_config_from_file()
                before = first.read_bytes()
                with patch("checksi.gui.config_editor.QFileDialog.getSaveFileName", return_value=(str(first), "")), \
                     patch("checksi.gui.config_editor.QMessageBox.question", return_value=QMessageBox.StandardButton.No) as question:
                    self.editor._save_config()
                self.assertEqual(question.call_count, 1)
                self.assertEqual(question.call_args.args[1], "Replace Configuration")
                self.assertEqual(first.read_bytes(), before)
                self.assertEqual(self.editor.build_config().dp5.timeout_seconds, 999)

    def test_deleted_source_can_be_recreated_only_with_explicit_consent(self) -> None:
        destination = self.root / "deleted.toml"
        ConfigManager(destination).save(ConfigManager.load_factory())
        with patch("checksi.gui.config_editor.QFileDialog.getOpenFileName", return_value=(str(destination), "")):
            self.editor._load_config_from_file()
        self.editor.default_output_mode_control.set_value("detailed")
        destination.unlink()
        statuses = []
        self.editor.status_message.connect(lambda level, text: statuses.append((level, text)))
        with patch("checksi.gui.config_editor.QFileDialog.getSaveFileName", return_value=(str(destination), "")), \
             patch("checksi.gui.config_editor.QMessageBox.question", return_value=QMessageBox.StandardButton.No) as question:
            self.editor._save_config()
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0][0], "critical")
        self.assertIn("deleted", statuses[0][1])
        self.assertNotIn("changed on disk", statuses[0][1])
        self.assertEqual(question.call_args.args[1], "Settings Changed")
        self.assertIn("recreate", question.call_args.args[2].lower())
        self.assertFalse(destination.exists())
        with patch("checksi.gui.config_editor.QFileDialog.getSaveFileName", return_value=(str(destination), "")), \
             patch("checksi.gui.config_editor.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes), \
             patch("checksi.gui.config_editor.QMessageBox.warning"):
            self.editor._save_config()
        self.assertTrue(destination.exists())
        self.assertEqual(ConfigManager(destination).load().modes.output_mode, "detailed")

    def test_recreation_consent_cannot_overwrite_a_concurrently_recreated_source(self) -> None:
        destination = self.root / "deleted.toml"
        ConfigManager(destination).save(ConfigManager.load_factory())
        with patch("checksi.gui.config_editor.QFileDialog.getOpenFileName", return_value=(str(destination), "")):
            self.editor._load_config_from_file()
        self.editor.default_output_mode_control.set_value("detailed")
        destination.unlink()
        questions = []

        def respond(_parent, title, message, *_args):
            questions.append((title, message))
            if len(questions) == 1:
                latest = ConfigManager.load_factory()
                latest.dp5.timeout_seconds = 903
                ConfigManager(destination).save(latest)
                return QMessageBox.StandardButton.Yes
            return QMessageBox.StandardButton.No

        with patch("checksi.gui.config_editor.QFileDialog.getSaveFileName", return_value=(str(destination), "")), \
             patch("checksi.gui.config_editor.QMessageBox.question", side_effect=respond):
            self.editor._save_config()
        self.assertEqual([title for title, _ in questions], ["Settings Changed", "Settings Changed"])
        self.assertIn("recreate", questions[0][1].lower())
        self.assertIn("Reload", questions[1][1])
        saved = ConfigManager(destination).load()
        self.assertEqual(saved.dp5.timeout_seconds, 903)
        self.assertEqual(saved.modes.output_mode, "essential")
        self.assertEqual(self.editor.build_config().modes.output_mode, "detailed")

    def test_theme_startup_reads_without_persisting_then_user_choice_saves(self) -> None:
        controller = ThemeController(self.app)
        controller._settings = Mock()
        controller._settings.value.return_value = "system"
        with patch("checksi.gui.theme.build_stylesheet", None):
            controller.apply()
            controller._settings.setValue.assert_not_called()
            controller.apply(ThemePreference.DARK)
        controller._settings.setValue.assert_called_once_with(controller.SETTINGS_KEY, "dark")

    def test_precise_loaded_margins_and_solvents_survive_until_edited(self) -> None:
        config = ConfigManager.load_factory()
        config.modes.header_margin = 0.4
        config.modes.footer_margin = 0.125
        config.solvents.entries[0].proton_shifts = [7.123456789012345, 0.0000001234567890123]
        config.solvents.entries[0].carbon_shifts = [123.12345678901234]
        self.editor.load_from_config(config)
        self.assertEqual(self.editor.build_config(), config)
        self.assertIn("keep 0.4", self.editor.pdf_header_margin_spinbox.toolTip())
        self.editor.pdf_header_margin_spinbox.setValue(20)
        rebuilt = self.editor.build_config()
        self.assertEqual(rebuilt.modes.header_margin, 0.2)
        self.assertEqual(rebuilt.modes.footer_margin, 0.125)
        self.assertEqual(self.editor.pdf_header_margin_spinbox.toolTip(), "")
        self.editor.pdf_footer_margin_slider.setValue(10)
        self.assertEqual(self.editor.build_config().modes.footer_margin, 0.1)
        self.editor.solvents_table.item(0, 2).setText("7.15")
        self.assertEqual(self.editor.build_config().solvents.entries[0].proton_shifts, [7.15])

    def test_cli_execution_and_gui_bootstrap_preserve_every_session_argument(self) -> None:
        from checksi.gui import app as bootstrap
        from checksi.application_runner import ApplicationRunner

        selected = self.root / "article.pdf"
        selected.touch()
        storage = self.root / "blob-0001"
        storage.touch()
        linked = self.root / "linked.pdf"
        try:
            linked.symlink_to(storage)
        except (OSError, NotImplementedError):
            linked = selected
        destination = self.root / "config" / "settings.toml"
        base = ConfigManager.load_factory()
        base.paths.input_path = selected
        base.paths.output_dir = Path("file-outputs")
        base.modes.deep_name_analysis = "on"
        base.modes.output_mode = "essential"
        base.dp5.enabled = True
        ConfigManager(destination).save(base)
        cases = [
            ([], selected, True),
            ([str(linked)], linked, True),
            ([str(selected), "--output-dir", "session-outputs"], selected, True),
            ([str(selected), "--deep-name-analysis", "off"], selected, True),
            ([str(selected), "--output-mode", "detailed"], selected, True),
            ([str(selected), "--dp5"], selected, True),
            ([str(selected), "--no-dp5"], selected, False),
            ([str(selected), "--override", "modes.header_margin=0.4",
              "--override", "modes.footer_margin=0.125",
              "--override", "solvents.entries.0.proton_shifts=[7.123456789012345]",
              "--override", "solvents.entries.0.carbon_shifts=[123.12345678901234]",
              "--override", "conversion.libreoffice_path=",
              "--override", "conversion.timeout_seconds=25"], selected, True),
        ]
        for session_arguments, expected_input, expected_dp5 in cases:
            with self.subTest(arguments=session_arguments):
                base.dp5.enabled = "--dp5" not in session_arguments
                ConfigManager(destination).save(base)
                original_file = destination.read_bytes()
                cli_configs = []
                gui_configs = []

                def execute(_runner, config, input_path, cancel_token):
                    cli_configs.append(config.model_copy(deep=True))
                    return SimpleNamespace(
                        input_path=input_path, analysis_data={}, report_data={},
                        issue_counts={"critical": 0, "warning": 0, "info": 0, "anomaly_count": 0},
                    )

                def refresh_available(window, *, show_message):
                    window.dashboard_page.set_dp5_runtime_status(True, [])
                    window.config_editor_page.set_dp5_runtime_status(True, [])

                def create_window(**kwargs):
                    window = CheckSIMainWindow(**kwargs)
                    self.addCleanup(window.close)
                    gui_configs.append(window._build_run_config())
                    return window

                gui_arguments = ["--config-file", str(destination), *session_arguments]
                cli_arguments = [*session_arguments] or [str(selected)]
                application = Mock(wraps=self.app)
                application.exec.return_value = 0
                theme = Mock()
                theme.load_preference.return_value = ThemePreference.SYSTEM
                with patch.object(ApplicationRunner, "execute", execute), redirect_stdout(StringIO()):
                    self.assertEqual(main.main_cli([
                        "--config-file", str(destination), "run", *cli_arguments,
                    ]), 0)
                with patch.object(bootstrap, "QApplication", return_value=application), \
                     patch.object(bootstrap, "ThemeController", return_value=theme), \
                     patch.object(bootstrap, "CheckSIMainWindow", side_effect=create_window), \
                     patch.object(CheckSIMainWindow, "_refresh_dp5_runtime_status", refresh_available):
                    self.assertEqual(bootstrap.main(["checksi-gui", *gui_arguments]), 0)
                self.assertEqual(len(cli_configs), 1)
                self.assertEqual(gui_configs, cli_configs)
                actual = gui_configs[0]
                self.assertEqual(actual.paths.input_path, expected_input)
                self.assertEqual(actual.dp5.enabled, expected_dp5)
                if "--output-dir" in session_arguments:
                    self.assertEqual(actual.paths.output_dir, Path.cwd() / "session-outputs")
                else:
                    self.assertEqual(actual.paths.output_dir, destination.parent / "file-outputs")
                if "--deep-name-analysis" in session_arguments:
                    self.assertEqual(actual.modes.deep_name_analysis, "off")
                if "--output-mode" in session_arguments:
                    self.assertEqual(actual.modes.output_mode, "detailed")
                if "--override" in session_arguments:
                    self.assertEqual(actual.modes.header_margin, 0.4)
                    self.assertEqual(actual.modes.footer_margin, 0.125)
                    self.assertEqual(actual.solvents.entries[0].proton_shifts, [7.123456789012345])
                    self.assertEqual(actual.solvents.entries[0].carbon_shifts, [123.12345678901234])
                    self.assertIsNone(actual.conversion.libreoffice_path)
                    self.assertEqual(actual.conversion.timeout_seconds, 25)
                self.assertEqual(destination.read_bytes(), original_file)


if __name__ == "__main__":
    unittest.main()
