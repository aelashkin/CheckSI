"""Acceptance tests for the concise single-document analysis workflow."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QSettings, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

from checksi.config import ConfigManager, resolve_config_paths
from checksi.gui.main_window import CheckSIMainWindow, RunState
from checksi.gui.theme import ThemeController, ThemePreference
from checksi.gui.worker import WorkerFailure


class GuiWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.input_a = self.root / "article-a.pdf"
        self.input_b = self.root / "article-b.pdf"
        self.input_a.write_bytes(b"display fixture")
        self.input_b.write_bytes(b"display fixture")
        self.html = self.root / "findings.html"
        self.html.write_text("<p>Fixture findings</p>", encoding="utf-8")
        self.cfg = resolve_config_paths(ConfigManager.load_factory())
        self.cfg.paths.input_path = self.input_a
        self.cfg.paths.output_dir = self.root / "results"
        self.cfg.modes.open_reports_on_success = False
        theme = Mock()
        theme.load_preference.return_value = ThemePreference.LIGHT
        self.enterContext(patch.object(CheckSIMainWindow, "_refresh_dp5_runtime_status"))
        self.window = CheckSIMainWindow(base_config=self.cfg, theme_controller=theme)
        self.window.show()
        self.app.processEvents()
        self.dashboard = self.window.dashboard_page
        self.addCleanup(self.close_window)

    def close_window(self):
        self.window._worker_thread = None
        self.window._dp5_status_thread = None
        self.window._shutdown_requested = True
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def result(self, *, partial=False):
        return SimpleNamespace(input_path=str(self.input_a),
            analysis_data={"total_found": 1, "assessment": {"status": "complete", "assessed_descriptions": 1, "unassessed": []}},
            report_data={"html_path": str(self.html), "output_dir": str(self.root),
                "partial": partial, "artifact_warnings": ["PDF unavailable: converter not found."] if partial else []},
            issue_counts={"critical": 1, "warning": 0, "info": 0, "anomaly_count": 1})

    def display_previous_result(self):
        result = self.result()
        self.window._last_result = result
        self.window._last_run_config = self.cfg.model_copy(deep=True)
        self.dashboard.show_summary(result, str(self.root))
        self.app.processEvents()
        return result

    def test_result_is_identified_after_another_document_is_selected(self):
        self.display_previous_result()
        self.dashboard.set_input_path(str(self.input_b))
        self.assertIn("article-a.pdf", self.dashboard.result_source_label.text())
        self.assertNotIn("article-b.pdf", self.dashboard.result_source_label.text())
        self.assertTrue(self.dashboard.open_html_button.isEnabled())

    def test_previous_artifact_actions_survive_a_failed_next_run(self):
        self.display_previous_result()
        self.dashboard.set_input_path(str(self.input_b))
        with patch("checksi.gui.main_window.QThread.start"):
            self.window._start_analysis()
        self.assertTrue(self.dashboard.summary_card.isVisible())
        with patch("checksi.gui.main_window.QMessageBox.critical"):
            self.window._handle_worker_failure(self.window._run_generation,
                WorkerFailure(message="Synthetic next-run failure", details=""))
        self.assertTrue(self.dashboard.open_html_button.isEnabled())
        self.assertIn("article-a.pdf", self.dashboard.result_source_label.text())

    def test_rechecking_the_same_file_marks_the_retained_result_as_previous(self):
        self.display_previous_result()
        with patch("checksi.gui.main_window.QThread.start"):
            self.window._start_analysis()
        self.assertIn("Previous result", self.dashboard.result_source_label.text())

    def test_valid_selected_input_does_not_depend_on_unused_default_input(self):
        self.window.settings_nav_button.click()
        editor = self.window.config_editor_page
        editor.input_path_edit.clear()
        with patch.object(editor, "request_confirmation", side_effect=editor.confirm_changes):
            self.window.analyze_nav_button.click()
        self.assertIs(self.window.page_stack.currentWidget(), self.dashboard)
        with patch("checksi.gui.main_window.QThread.start"):
            self.window._start_analysis()
        self.assertEqual(self.window._last_run_config.paths.input_path, self.input_a)

    def test_cancel_state_remains_visible_until_the_worker_finishes(self):
        self.window._cancel_token = threading.Event()
        self.window._set_run_state(RunState.RUNNING)
        QTest.keyClick(self.window.global_cancel_button, Qt.Key.Key_Space)
        self.assertTrue(self.window._cancel_token.is_set())
        self.assertTrue(self.window.global_cancel_button.isVisible())
        self.assertIn("Cancelling", self.window.global_cancel_button.text())
        self.assertFalse(self.window.global_cancel_button.isEnabled())
        self.assertIn("Cancelling", self.window.run_status_label.text())

    def test_settings_edits_during_a_run_are_identified_as_next_run_options(self):
        with patch("checksi.gui.main_window.QThread.start"):
            self.window._start_analysis()
        self.window.settings_nav_button.click()
        editor = self.window.config_editor_page
        on_button = next(button for button in editor.default_deep_name_analysis_control.findChildren(QPushButton)
            if button.text() == "On")
        on_button.click()
        self.assertEqual(self.window._last_run_config.modes.deep_name_analysis.value, "off")
        self.assertIn("Next run", self.dashboard.options_section.toggle_button.text())
        self.assertIn("next run", editor.config_persistence_label.text())
        self.window._handle_progress_event(self.window._run_generation, 3, 12, "Checking descriptions")
        self.assertIn("Off", self.window.run_status_label.text())
        self.global_cancel_from_settings()

    def global_cancel_from_settings(self):
        self.assertTrue(self.window.global_cancel_button.isVisible())
        QTest.keyClick(self.window.global_cancel_button, Qt.Key.Key_Space)
        self.assertTrue(self.window._cancel_token.is_set())

    def test_phase_counts_remain_readable_outside_the_filled_bar(self):
        self.dashboard.set_run_state(RunState.RUNNING)
        self.dashboard.set_progress(3, 12, "Checking descriptions")
        self.assertTrue(self.dashboard.progress_count_label.isVisible())
        self.assertEqual(self.dashboard.progress_count_label.text(), "3 / 12")
        self.dashboard.set_progress(0, 0, "Extracting document")
        self.assertTrue(self.dashboard.progress_count_label.isHidden())

    def test_partial_export_reason_is_visible_without_hover(self):
        self.dashboard.show_summary(self.result(partial=True), str(self.root))
        self.assertTrue(self.dashboard.artifact_warning_label.isVisible())
        self.assertIn("converter not found", self.dashboard.artifact_warning_label.text())

    def test_log_reading_position_survives_new_messages(self):
        self.dashboard.set_run_state(RunState.RUNNING)
        for i in range(100):
            self.dashboard.append_log("warning", f"Earlier warning {i}")
        self.app.processEvents()
        scrollbar = self.dashboard.log_output.verticalScrollBar()
        self.assertGreater(scrollbar.maximum(), 0)
        scrollbar.setValue(0)
        self.dashboard.append_log("warning", "A newly arriving warning")
        self.app.processEvents()
        self.assertEqual(scrollbar.value(), 0)

    def test_output_parent_is_visible_before_running(self):
        self.assertEqual(self.dashboard.output_dir_edit.text(), str(self.cfg.paths.output_dir))
        self.assertTrue(self.dashboard.output_dir_edit.isVisible())

    def test_actual_log_foregrounds_follow_theme_for_existing_messages(self):
        previous = self.app.property("themeDark")
        self.addCleanup(self.app.setProperty, "themeDark", previous)
        self.app.setProperty("themeDark", False)
        self.dashboard.append_log("warning", "A warning that remains after switching theme")
        fragment = self.dashboard.log_output.document().firstBlock().begin().fragment()
        self.assertEqual(fragment.charFormat().foreground().color().name(),
            ThemeController.colors(False)["warning_text_on_surface"].lower())
        self.app.setProperty("themeDark", True)
        self.dashboard.refresh_log_colors()
        fragment = self.dashboard.log_output.document().firstBlock().begin().fragment()
        self.assertEqual(fragment.charFormat().foreground().color().name(),
            ThemeController.colors(True)["warning_text_on_surface"].lower())
        self.assertIn("[Warning]", self.dashboard.log_output.toPlainText())

    def test_native_deleted_window_is_not_a_system_theme_observer(self):
        theme = ThemeController(self.app)
        theme._settings = QSettings(str(self.root / "appearance.ini"), QSettings.Format.IniFormat)
        window = CheckSIMainWindow(base_config=self.cfg, theme_controller=theme)
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        with patch.object(theme, "apply") as apply:
            self.app.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Dark)
        apply.assert_not_called()

    def test_invalid_solvent_moves_to_settings_and_reveals_its_cell(self):
        editor = self.window.config_editor_page
        editor.solvents_table.item(0, 2).setText("not-a-shift")
        with patch.object(editor, "request_confirmation", side_effect=editor.confirm_changes):
            self.window._start_analysis()
        self.app.processEvents()
        self.assertIs(self.window.page_stack.currentWidget(), editor)
        self.assertTrue(editor.solvents_section.is_expanded())
        self.assertEqual(editor.solvents_table.currentColumn(), 2)
        self.assertEqual(editor.solvents_table.currentRow(), 0)
        self.assertTrue(editor.validation_error_label.isVisible())

    def test_findings_action_is_inside_laptop_viewport_after_completion(self):
        self.window.resize(1024, 700)
        self.display_previous_result()
        self.app.processEvents()
        button = self.dashboard.open_html_button
        viewport = self.dashboard._scroll_area.viewport()
        origin = button.mapTo(viewport, button.rect().topLeft())
        self.assertGreaterEqual(origin.y(), 0)
        self.assertLessEqual(origin.y() + button.height(), viewport.height())

    def test_expanding_activity_reveals_the_readable_log_in_laptop_viewport(self):
        self.window.resize(1024, 700)
        self.display_previous_result()
        self.dashboard.activity_details.toggle_button.click()
        QTest.qWait(10)
        log = self.dashboard.log_output
        viewport = self.dashboard._scroll_area.viewport()
        origin = log.mapTo(viewport, log.rect().topLeft())
        self.assertGreaterEqual(origin.y(), 0)
        self.assertLessEqual(origin.y() + log.height(), viewport.height())

    def test_editing_a_truncated_setting_discards_the_hidden_original(self):
        config = self.cfg.model_copy(deep=True)
        config.conversion.libreoffice_path = "x" * 40000
        editor = self.window.config_editor_page
        editor.load_from_config(config)
        displayed = editor.libreoffice_path_edit.text()
        self.assertLess(len(displayed), 40000)
        editor.libreoffice_path_edit.setText("replacement")
        editor.libreoffice_path_edit.setText(displayed)
        self.assertEqual(editor.build_config().conversion.libreoffice_path, displayed)

    def test_setting_mode_edit_updates_the_compact_run_summary(self):
        control = self.window.config_editor_page.default_deep_name_analysis_control
        on_button = next(button for button in control.findChildren(QPushButton) if button.text() == "On")
        on_button.click()
        self.assertIn("Off", self.dashboard.options_section.toggle_button.text())
        self.assertTrue(self.window.config_editor_page.confirm_changes())
        self.assertIn("On", self.dashboard.options_section.toggle_button.text())
        self.assertIn("local OPSIN", self.dashboard.options_help.text())

    def test_retained_output_folder_action_does_not_use_next_run_destination(self):
        self.display_previous_result()
        self.window._last_run_config.paths.output_dir = self.root / "next-run"
        with patch("checksi.gui.main_window.QDesktopServices.openUrl", return_value=True) as open_url:
            self.window._open_output_folder()
        self.assertEqual(Path(open_url.call_args.args[0].toLocalFile()).resolve(), self.root.resolve())

    def test_file_open_failure_offers_a_working_copy_path_action(self):
        self.display_previous_result()
        seen = []
        def click_copy():
            dialog = next(widget for widget in self.app.topLevelWidgets()
                if isinstance(widget, QMessageBox) and widget.isVisible())
            seen.append(dialog.text())
            next(button for button in dialog.buttons() if button.text() == "Copy Path").click()
        deadline = QTimer(self.window)
        deadline.setSingleShot(True)
        deadline.timeout.connect(lambda: [widget.reject() for widget in self.app.topLevelWidgets()
            if isinstance(widget, QMessageBox) and widget.isVisible()])
        with patch("checksi.gui.main_window.QDesktopServices.openUrl", return_value=False), \
             patch("checksi.gui.main_window.QApplication.clipboard") as clipboard:
            QTimer.singleShot(0, click_copy)
            deadline.start(1000)
            self.window._open_summary_artifact("html_path")
            deadline.stop()
        self.assertIn(str(self.html), seen[0])
        clipboard.return_value.setText.assert_called_once_with(str(self.html))

    def test_standard_open_shortcut_selects_a_document(self):
        self.window.activateWindow()
        self.window.setFocus()
        self.app.processEvents()
        with patch("checksi.gui.main_window.QFileDialog.getOpenFileName", return_value=(str(self.input_b), "")):
            combination = self.window._open_shortcut.key()[0]
            QTest.keyClick(self.window, combination.key(), combination.keyboardModifiers())
        self.assertEqual(self.dashboard.input_path(), str(self.input_b))


if __name__ == "__main__":
    unittest.main()
