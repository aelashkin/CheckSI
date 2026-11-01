"""Real Settings workflows protecting the PR 46 review repairs."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox

from checksi.config import ConfigIOError, ConfigManager, resolve_config_paths
from checksi.gui.main_window import CheckSIMainWindow, RunState
from checksi.gui.theme import ThemePreference


class SettingsReviewResponseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.default_input = self.root / "default.pdf"
        self.selected_input = self.root / "selected.pdf"
        self.default_input.write_bytes(b"default document fixture")
        self.selected_input.write_bytes(b"selected document fixture")
        self.user_defaults = self.root / "user-defaults.toml"
        self.config = resolve_config_paths(ConfigManager.load_factory())
        self.config.paths.input_path = self.default_input
        self.config.paths.output_dir = self.root / "results"
        self.config.modes.open_reports_on_success = False
        self.enterContext(patch("checksi.gui.config_editor.user_config_path",
                                return_value=self.user_defaults))
        self.enterContext(patch.object(CheckSIMainWindow, "_refresh_dp5_runtime_status"))
        theme = Mock()
        theme.load_preference.return_value = ThemePreference.LIGHT
        self.window = CheckSIMainWindow(base_config=self.config, theme_controller=theme)
        self.editor = self.window.config_editor_page
        self.dashboard = self.window.dashboard_page
        self.window.show()
        self.app.processEvents()
        self.addCleanup(self._close_window)

    def _close_window(self):
        self.window._worker_thread = None
        self.window._dp5_status_thread = None
        self.window._shutdown_requested = True
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def _choose_dialog_button(self, text, action):
        """Drive a real modal and report timer errors outside its callback."""
        errors = []
        shown = []
        choose = QTimer()
        choose.setSingleShot(True)
        deadline = QTimer()
        deadline.setSingleShot(True)

        def reject_dialog(message):
            errors.append(message)
            dialog = self.app.activeModalWidget()
            if isinstance(dialog, QDialog):
                dialog.reject()

        def click_button():
            dialog = self.app.activeModalWidget()
            if not isinstance(dialog, QMessageBox):
                reject_dialog("The expected Settings message box was not shown.")
                return
            shown.append(dialog.windowTitle())
            button = next((button for button in dialog.buttons() if button.text() == text), None)
            if button is None:
                reject_dialog(f"Missing {text!r}; available buttons: {[button.text() for button in dialog.buttons()]}")
                return
            button.click()

        choose.timeout.connect(click_button)
        deadline.timeout.connect(lambda: reject_dialog("Settings dialog did not close within one second."))
        choose.start(0)
        deadline.start(1000)
        try:
            result = action()
        finally:
            choose.stop()
            deadline.stop()
        self.assertEqual(errors, [])
        self.assertEqual(len(shown), 1, "Expected one Settings message box.")
        return result

    def test_blank_default_input_confirms_then_runs_selected_document_with_other_edits(self):
        with patch("checksi.gui.main_window.QFileDialog.getOpenFileName",
                   return_value=(str(self.selected_input), "Documents (*.pdf *.docx)")):
            self.dashboard.browse_button.click()
        self.window.settings_nav_button.click()
        self.editor.input_path_edit.clear()
        next_output = self.root / "new-results"
        self.editor.output_dir_edit.setText(str(next_output))

        self._choose_dialog_button("Confirm Changes", self.window.analyze_nav_button.click)

        self.assertIs(self.window.page_stack.currentWidget(), self.dashboard)
        self.assertFalse(self.editor.has_unconfirmed_changes())
        self.assertEqual(self.editor.confirmed_config().paths.input_path, self.default_input)
        self.assertEqual(self.dashboard.input_path(), str(self.selected_input))
        with patch("checksi.gui.main_window.QThread.start") as start:
            self.dashboard.run_button.click()
        start.assert_called_once()
        self.assertEqual(self.window._last_run_config.paths.input_path, self.selected_input)
        self.assertEqual(self.window._last_run_config.paths.output_dir, next_output)
        self.assertEqual(self.window._worker._config.paths.input_path, self.selected_input)
        self.assertEqual(self.window._worker._config.paths.output_dir, next_output)

    def test_discard_invalid_draft_preserves_unsaved_confirmation_and_active_run(self):
        self.window.settings_nav_button.click()
        self.editor.open_reports_checkbox.toggle()
        self.editor.confirm_button.click()
        confirmed = self.editor.confirmed_config()
        self.assertTrue(self.editor.has_unsaved_changes())
        self.window.analyze_nav_button.click()
        with patch("checksi.gui.main_window.QThread.start"):
            self.dashboard.run_button.click()
        frozen = self.window._last_run_config.model_copy(deep=True)
        self.window.settings_nav_button.click()
        self.editor.output_dir_edit.clear()
        self.assertFalse(self.editor.confirm_changes())
        self.assertFalse(self.editor.validation_error_label.isHidden())

        self._choose_dialog_button("Discard Changes", self.window.view_run_button.click)

        self.assertIs(self.window.page_stack.currentWidget(), self.dashboard)
        self.assertEqual(self.editor.confirmed_config(), confirmed)
        self.assertTrue(self.editor.validation_error_label.isHidden())
        self.assertEqual(self.editor.build_config(), confirmed)
        self.assertFalse(self.editor.has_unconfirmed_changes())
        self.assertTrue(self.editor.has_unsaved_changes(), "Discard must retain the original saved baseline.")
        self.assertEqual(self.dashboard.output_dir_edit.text(), str(confirmed.paths.output_dir))
        self.assertEqual(self.window._last_run_config, frozen)
        self.assertEqual(self.window._worker._config, frozen)
        self.assertFalse(self.user_defaults.exists())

    def test_export_names_saved_file_and_explains_startup_without_close_warning(self):
        self.window.settings_nav_button.click()
        self.editor.open_reports_checkbox.toggle()
        exported = self.root / "exported-settings.toml"
        with patch("checksi.gui.config_editor.QFileDialog.getSaveFileName",
                   return_value=(str(exported), "TOML Files (*.toml)")):
            self.editor.save_config_button.click()
        self.assertEqual(ConfigManager(exported).load(), self.editor.confirmed_config())
        self.assertFalse(self.editor.has_unsaved_changes())
        status = self.editor.config_persistence_label.text()
        self.assertIn(str(exported), status)
        self.assertIn("default", status.lower())
        self.assertIn("load", status.lower())
        self.assertTrue(any(word in status.lower() for word in ("startup", "restart", "launch")), status)
        event = QCloseEvent()
        with patch.object(QMessageBox, "exec", return_value=QDialog.DialogCode.Rejected) as warning:
            self.window.closeEvent(event)
        warning.assert_not_called()
        self.assertTrue(event.isAccepted())
        self.assertFalse(self.user_defaults.exists())

    def test_startup_session_options_do_not_claim_to_be_saved_in_the_source_file(self):
        source = self.root / "startup.toml"
        manager = ConfigManager(source)
        manager.save(self.config)
        session = self.config.model_copy(deep=True)
        session.dp5.timeout_seconds = 777
        self.editor.load_from_config(session)
        self.editor.set_config_source(manager)
        status = self.editor.config_persistence_label.text()
        self.assertIn(str(source), status)
        self.assertNotIn("Saved in", status)
        self.assertEqual(ConfigManager(source).load(), self.config)
        self.assertEqual(self.editor.confirmed_config(), session)

    def test_discard_preserves_saved_export_equivalence_after_normalizing_widgets(self):
        for changed_field in ("blank default input", "solvent aliases", "input path", "output path"):
            with self.subTest(changed_field=changed_field):
                self.editor.load_from_config(self.config)
                if changed_field == "blank default input":
                    self.editor.input_path_edit.clear()
                elif changed_field == "solvent aliases":
                    self.editor.solvents_table.item(0, 1).setText("new alias")
                elif changed_field == "input path":
                    self.editor.input_path_edit.setText(str(self.default_input) + "/")
                else:
                    self.editor.output_dir_edit.setText(str(self.root / "new-results") + "/./")
                exported = self.root / f"{changed_field}.toml"
                with patch("checksi.gui.config_editor.QFileDialog.getSaveFileName",
                           return_value=(str(exported), "")):
                    self.editor._save_config()
                saved = self.editor.confirmed_config()
                self.assertFalse(self.editor.has_unsaved_changes())
                self.editor.output_dir_edit.clear()
                self._choose_dialog_button("Discard Changes", self.editor.request_confirmation)
                self.assertEqual(self.editor.build_config(), saved)
                self.assertFalse(self.editor.has_unsaved_changes())

    def test_discard_factory_draft_restores_the_file_source_label(self):
        source = self.root / "original.toml"
        config = self.config.model_copy(deep=True)
        config.dp5.timeout_seconds = 777
        manager = ConfigManager(source)
        manager.save(config)
        self.editor.load_from_config(config)
        self.editor.set_config_source(manager)
        self.editor._restore_default_config()
        self.assertIn("factory", self.editor.config_source_label.text())
        self._choose_dialog_button("Discard Changes", self.editor.request_confirmation)
        self.assertEqual(self.editor.build_config(), config)
        self.assertIn(str(source), self.editor.config_source_label.text())
        self.assertFalse(self.editor.has_unsaved_changes())

    def test_path_labels_are_vertically_centered_on_their_fields(self):
        self.window.settings_nav_button.click()
        self.app.processEvents()
        for field in (self.editor.input_path_edit, self.editor.output_dir_edit):
            with self.subTest(field=field.accessibleName()):
                label = next(label for label in self.editor.findChildren(QLabel) if label.buddy() is field)
                label_center = label.mapTo(self.editor, label.rect().center()).y()
                field_center = field.mapTo(self.editor, field.rect().center()).y()
                self.assertLessEqual(abs(label_center - field_center), 1)

    def test_close_keep_editing_keeps_window_enabled_and_shows_settings(self):
        self.editor.open_reports_checkbox.toggle()
        event = QCloseEvent()

        self._choose_dialog_button("Keep Editing", lambda: self.window.closeEvent(event))

        self.assertFalse(event.isAccepted())
        self.assertFalse(self.window._shutdown_requested)
        self.assertTrue(self.window.centralWidget().isEnabled())
        self.assertTrue(self.editor.isEnabled())
        self.assertIs(self.window.page_stack.currentWidget(), self.editor)
        self.assertTrue(self.window.settings_nav_button.isChecked())
        self.assertTrue(self.editor.has_unconfirmed_changes())

    def test_close_save_failure_does_not_commit_shutdown(self):
        self.editor.open_reports_checkbox.toggle()
        event = QCloseEvent()
        with patch.object(ConfigManager, "save", side_effect=ConfigIOError("Synthetic write failure")) as save, \
             patch("checksi.gui.config_editor.QMessageBox.warning") as warning:
            self._choose_dialog_button("Save User Defaults", lambda: self.window.closeEvent(event))
        save.assert_called_once()
        warning.assert_called_once()
        self.assertFalse(event.isAccepted())
        self.assertFalse(self.window._shutdown_requested)
        self.assertTrue(self.window.centralWidget().isEnabled())
        self.assertIs(self.window.page_stack.currentWidget(), self.editor)
        self.assertTrue(self.editor.has_unsaved_changes())
        self.assertFalse(self.user_defaults.exists())

    def test_deferred_close_disables_window_and_final_close_does_not_prompt_again(self):
        self.editor.open_reports_checkbox.toggle()
        self.assertTrue(self.editor.confirm_changes())
        with patch("checksi.gui.main_window.QThread.start"):
            self.dashboard.run_button.click()
        token = self.window._cancel_token
        first = QCloseEvent()
        self._choose_dialog_button("Close Without Saving", lambda: self.window.closeEvent(first))
        self.assertFalse(first.isAccepted())
        self.assertTrue(self.window._shutdown_requested)
        self.assertFalse(self.window.centralWidget().isEnabled())
        self.assertTrue(token.is_set())
        # The externally held worker has now completed its teardown.
        self.window._worker_thread = None
        final = QCloseEvent()
        with patch.object(QMessageBox, "exec", return_value=QDialog.DialogCode.Rejected) as prompt:
            self.window.closeEvent(final)
        prompt.assert_not_called()
        self.assertTrue(final.isAccepted())

    def test_declining_start_confirmation_does_not_create_or_start_worker(self):
        self.window.settings_nav_button.click()
        self.editor.open_reports_checkbox.toggle()
        with patch("checksi.gui.main_window.QThread.start") as start:
            self._choose_dialog_button("Keep Editing", self.window._start_analysis)
        start.assert_not_called()
        self.assertIsNone(self.window._worker_thread)
        self.assertIsNone(self.window._worker)
        self.assertIsNone(self.window._last_run_config)
        self.assertEqual(self.window._run_state, RunState.IDLE)
        self.assertIs(self.window.page_stack.currentWidget(), self.editor)
        self.assertTrue(self.editor.has_unconfirmed_changes())


if __name__ == "__main__":
    unittest.main()
