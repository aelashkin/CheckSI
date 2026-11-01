"""Search, confirmation and persistence acceptance tests for Settings."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QTimer, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QCheckBox, QLineEdit, QMessageBox, QVBoxLayout, QWidget

from checksi.config import ConfigManager, resolve_config_paths
from checksi.errors.defs import AnalysisErrorCode
from checksi.gui.config_editor import ConfigEditorPage
from checksi.gui.main_window import CheckSIMainWindow
from checksi.gui.theme import ThemeController, ThemePreference


class SettingsUxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.config = resolve_config_paths(ConfigManager.load_factory())
        self.editor = ConfigEditorPage()
        self.editor.load_from_config(self.config)
        self.addCleanup(self.editor.deleteLater)

    def visible_codes(self):
        return {self.editor.errors_table.item(row, 0).text()
                for row in range(self.editor.errors_table.rowCount())
                if not self.editor.errors_table.isRowHidden(row)}

    def test_live_search_matches_codes_and_every_message_case_insensitively(self):
        before = self.editor.build_config()
        self.editor.error_search_edit.setText("CHARGE")
        self.assertIn("hrms_ion_charge_missing", self.visible_codes())
        self.assertIn("hrms_formula_charge_missing", self.visible_codes())
        self.assertNotIn("c13_section_missing", self.visible_codes())
        self.editor.error_search_edit.setText("13c")
        self.assertIn("c13_section_missing", self.visible_codes())
        self.assertIn("nmr_signal_format", self.visible_codes())
        self.assertTrue({code.value for code in AnalysisErrorCode if code.value.startswith("c13_")}
                        <= self.visible_codes())
        self.editor.error_search_edit.setText("represented carbon shifts")
        self.assertEqual(self.visible_codes(), {"nmr_signal_format"})
        self.editor.error_search_edit.setText("no matching diagnostic")
        self.assertFalse(self.visible_codes())
        self.assertIn("No", self.editor.error_filter_status.text())
        self.editor.error_search_edit.clear()
        self.assertEqual(len(self.visible_codes()), len(AnalysisErrorCode))
        self.assertEqual(self.editor.build_config(), before)
        self.assertFalse(self.editor.has_unconfirmed_changes())

    def test_changed_filter_tracks_factory_differences_and_preserves_hidden_edits(self):
        self.editor.changed_errors_checkbox.setChecked(True)
        self.assertFalse(self.visible_codes())
        row = list(AnalysisErrorCode).index(AnalysisErrorCode.HRMS_ION_CHARGE_MISSING)
        item = self.editor.errors_table.item(row, 1)
        item.setData(Qt.ItemDataRole.UserRole, "disabled")
        self.assertEqual(self.visible_codes(), {"hrms_ion_charge_missing"})
        self.editor.error_search_edit.setText("13C")
        self.assertFalse(self.visible_codes())
        self.assertIn(AnalysisErrorCode.HRMS_ION_CHARGE_MISSING,
                      self.editor.build_config().errors.overrides)
        self.editor.error_search_edit.clear()
        item.setData(Qt.ItemDataRole.UserRole, None)
        self.assertFalse(self.visible_codes())
        self.assertFalse(self.editor.has_unconfirmed_changes())
        factory_code = AnalysisErrorCode.C13_BORON_SIGNAL_NOT_OBSERVED
        row = list(AnalysisErrorCode).index(factory_code)
        factory_item = self.editor.errors_table.item(row, 1)
        original = factory_item.data(Qt.ItemDataRole.UserRole)
        factory_item.setData(Qt.ItemDataRole.UserRole, None)
        self.assertEqual(self.visible_codes(), {factory_code.value})
        self.assertNotIn(factory_code, self.editor.build_config().errors.overrides)
        factory_item.setData(Qt.ItemDataRole.UserRole, original)
        self.assertFalse(self.visible_codes())

    def test_confirmation_is_explicit_and_does_not_save_a_file(self):
        self.assertEqual(self.editor.load_button.text(), "Import Config")
        self.assertEqual(self.editor.save_config_button.text(), "Export Config")
        self.assertFalse(self.editor.confirm_button.isEnabled())
        self.editor.open_reports_checkbox.setChecked(not self.config.modes.open_reports_on_success)
        self.assertTrue(self.editor.has_unconfirmed_changes())
        self.assertTrue(self.editor.confirm_button.isEnabled())
        self.assertEqual(self.editor.confirmed_config(), self.config)
        with patch.object(ConfigManager, "save") as save:
            self.assertTrue(self.editor.confirm_changes())
        save.assert_not_called()
        self.assertFalse(self.editor.has_unconfirmed_changes())
        self.assertTrue(self.editor.has_unsaved_changes())
        self.assertIn("not saved", self.editor.config_persistence_label.text())
        self.assertEqual(self.editor.confirmed_config(), self.editor.build_config())

    def test_invalid_confirmation_keeps_the_draft_and_confirmed_session(self):
        self.editor.output_dir_edit.clear()
        self.assertFalse(self.editor.confirm_changes())
        self.assertTrue(self.editor.has_unconfirmed_changes())
        self.assertEqual(self.editor.confirmed_config(), self.config)
        self.assertFalse(self.editor.validation_error_label.isHidden())

    def choose_dialog_button(self, text):
        errors = []

        def choose():
            dialog = self.app.activeModalWidget()
            if not isinstance(dialog, QMessageBox):
                errors.append("Expected a settings message box.")
                return
            button = next((button for button in dialog.buttons() if button.text() == text), None)
            if button is None:
                errors.append(f"Missing dialog button: {text}")
                dialog.reject()
            else:
                button.click()

        self.addCleanup(self.assertEqual, errors, [], f"Could not choose {text!r}")
        QTimer.singleShot(0, choose)

    def test_confirmation_popup_keeps_draft_or_applies_without_persisting(self):
        self.editor.open_reports_checkbox.toggle()
        self.choose_dialog_button("Keep Editing")
        self.assertFalse(self.editor.request_confirmation())
        self.assertTrue(self.editor.has_unconfirmed_changes())
        self.assertEqual(self.editor.confirmed_config(), self.config)
        self.choose_dialog_button("Confirm Changes")
        with patch.object(ConfigManager, "save") as save:
            self.assertTrue(self.editor.request_confirmation())
        save.assert_not_called()
        self.assertFalse(self.editor.has_unconfirmed_changes())

    def test_exit_prompt_can_keep_editing_discard_or_save_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "settings.toml"
            self.editor.open_reports_checkbox.toggle()
            self.assertTrue(self.editor.confirm_changes())
            self.choose_dialog_button("Keep Editing")
            self.assertFalse(self.editor.request_close())
            self.choose_dialog_button("Close Without Saving")
            self.assertTrue(self.editor.request_close())
            self.assertFalse(destination.exists())
            self.choose_dialog_button("Save User Defaults")
            with patch("checksi.gui.config_editor.user_config_path", return_value=destination):
                self.assertTrue(self.editor.request_close())
            self.assertEqual(ConfigManager(destination).load(), self.editor.confirmed_config())
            self.assertFalse(self.editor.has_unsaved_changes())

    def test_import_defaults_reloads_the_file_after_saving_the_current_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "settings.toml"
            ConfigManager(destination).save(self.config)
            self.editor.open_reports_checkbox.toggle()
            expected = self.editor.build_config()
            self.choose_dialog_button("Save User Defaults")
            with patch("checksi.gui.config_editor.QFileDialog.getOpenFileName",
                       return_value=(str(destination), "")), \
                 patch("checksi.gui.config_editor.user_config_path", return_value=destination):
                self.editor._load_config_from_file()
            self.assertEqual(self.editor.build_config(), expected)
            self.assertEqual(ConfigManager(destination).load(), expected)
            self.assertFalse(self.editor.has_unsaved_changes())
            # A later save must use the revision observed after our own write.
            with patch("checksi.gui.config_editor.QMessageBox.question") as conflict:
                self.assertTrue(self.editor._save_with_conflict_check(
                    self.editor._source_manager, expected, "Export Config"))
            conflict.assert_not_called()

    def test_restored_settings_remain_drafts_until_confirmed(self):
        changed = self.config.model_copy(deep=True)
        changed.dp5.timeout_seconds = 777
        self.editor.load_from_config(changed)
        self.editor._restore_default_config()
        self.assertEqual(self.editor.confirmed_config(), changed)
        self.assertTrue(self.editor.has_unconfirmed_changes())
        self.assertTrue(self.editor.confirm_changes())
        self.assertEqual(self.editor.confirmed_config(), self.config)

    def test_replacement_prompt_preserves_unconfirmed_and_confirmed_unsaved_edits(self):
        for confirmed in (False, True):
            with self.subTest(confirmed=confirmed):
                self.editor.load_from_config(self.config)
                self.editor.open_reports_checkbox.toggle()
                draft = self.editor.build_config()
                if confirmed:
                    self.assertTrue(self.editor.confirm_changes())
                self.choose_dialog_button("Keep Editing")
                self.editor._restore_default_config()
                self.assertEqual(self.editor.build_config(), draft)
                self.choose_dialog_button("Replace Settings")
                self.editor._restore_default_config()
                self.assertEqual(self.editor.build_config(), self.config)

    def test_close_commit_disables_edits_while_waiting_for_a_worker(self):
        theme = Mock()
        theme.load_preference.return_value = ThemePreference.LIGHT
        with patch.object(CheckSIMainWindow, "_refresh_dp5_runtime_status"):
            window = CheckSIMainWindow(base_config=self.config, theme_controller=theme)
        self.addCleanup(window.deleteLater)
        window._dp5_status_thread = Mock()
        event = QCloseEvent()
        window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertFalse(window.config_editor_page.isEnabled())
        self.assertTrue(window._shutdown_requested)
        window._dp5_status_thread = None

    def test_checkbox_keyboard_focus_is_visible_only_around_the_indicator(self):
        surface = QWidget()
        layout = QVBoxLayout(surface)
        checkbox = QCheckBox("Open generated reports on success")
        other = QLineEdit()
        layout.addWidget(checkbox)
        layout.addWidget(other)
        self.addCleanup(surface.deleteLater)
        surface.show()
        surface.activateWindow()
        for dark in (False, True):
            surface.setStyleSheet(ThemeController._custom_overrides(dark))
            for checked in (False, True):
                with self.subTest(dark=dark, checked=checked):
                    checkbox.setChecked(checked)
                    other.setFocus()
                    self.app.processEvents()
                    before = checkbox.grab().toImage()
                    checkbox.setFocus(Qt.FocusReason.TabFocusReason)
                    self.app.processEvents()
                    after = checkbox.grab().toImage()
                    self.assertTrue(checkbox.hasFocus())
                    self.assertNotEqual(before, after)
                    ratio = after.devicePixelRatio()
                    text_region = before.rect().adjusted(int(40 * ratio), 0, 0, 0)
                    self.assertEqual(before.copy(text_region), after.copy(text_region))

    def test_settings_navigation_requires_confirmation_and_cancel_keeps_edits(self):
        theme = Mock()
        theme.load_preference.return_value = ThemePreference.LIGHT
        with patch.object(CheckSIMainWindow, "_refresh_dp5_runtime_status"):
            window = CheckSIMainWindow(base_config=self.config, theme_controller=theme)
        self.addCleanup(window.deleteLater)
        window.settings_nav_button.click()
        editor = window.config_editor_page
        editor.output_dir_edit.setText("/tmp/edited-output")
        self.assertEqual(window.dashboard_page.output_dir_edit.text(), str(self.config.paths.output_dir))
        with patch.object(editor, "request_confirmation", return_value=False) as prompt:
            window.analyze_nav_button.click()
        prompt.assert_called_once()
        self.assertIs(window.page_stack.currentWidget(), editor)
        self.assertTrue(window.settings_nav_button.isChecked())
        self.assertEqual(editor.output_dir_edit.text(), "/tmp/edited-output")
        self.assertTrue(editor.confirm_changes())
        window.analyze_nav_button.click()
        self.assertIs(window.page_stack.currentWidget(), window.dashboard_page)
        self.assertEqual(window.dashboard_page.output_dir_edit.text(), "/tmp/edited-output")

    def test_grouped_path_fields_share_the_same_left_edge(self):
        self.editor.resize(1000, 700)
        self.editor.show()
        self.app.processEvents()
        self.assertEqual(self.editor.input_path_edit.mapTo(self.editor, QPoint()).x(),
                         self.editor.output_dir_edit.mapTo(self.editor, QPoint()).x())
        self.editor.hide()


if __name__ == "__main__":
    unittest.main()
