"""Offscreen Qt lifecycle checks with external work held at deterministic gates."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import warnings
from pathlib import Path
from unittest.mock import Mock, patch

from docx import Document

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QThread, QTimer, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication

from checksi.application_runner import ApplicationRunResult
from checksi.config import ConfigManager
from checksi.config_schema import AppConfig, ErrorSeverity
from checksi.events import LogEvent, ProgressEvent
from checksi.dp5q.client import DP5qAvailabilityError as DP5RuntimeValidationError
from checksi.gui.main_window import CheckSIMainWindow, RunState
from checksi.gui.theme import ThemePreference
from checksi.gui.worker import AnalysisWorker, QtEventBridge, WorkerFailure


class GuiLifecycleTests(unittest.TestCase):
    """Keep workers real while replacing network, runtime, and document boundaries."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self) -> None:
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.input_path = Path(self.temp) / "sample.docx"
        document = Document()
        document.add_paragraph("General methods.")
        document.save(self.input_path)
        self.cfg = AppConfig.model_validate(
            {
                "paths": {
                    "input_path": self.input_path,
                    "output_dir": Path(self.temp) / "report.docx",
                },
                "modes": {"deep_name_analysis": "off", "output_mode": "detailed"},
                "solvents": {"entries": []},
                "errors": {"default_severity": "warning"},
                "dp5": {"enabled": True},
            }
        )
        for method in ("critical", "warning", "information"):
            self.enterContext(patch(f"checksi.gui.main_window.QMessageBox.{method}"))
        self.availability = self.enterContext(
            patch("checksi.gui.worker.check_availability", return_value={"available": True, "model_id": "test-model", "score_version": "test-score"})
        )
        self.convert = self.enterContext(
            patch("checksi.application_runner.convert_docx_to_pdf", return_value=None)
        )
        theme = Mock()
        theme.load_preference.return_value = ThemePreference.SYSTEM
        self.window = CheckSIMainWindow(base_config=self.cfg, theme_controller=theme)
        self.gates: list[threading.Event] = []
        self._wait_for(lambda: self.window._dp5_status_thread is None)

    def tearDown(self) -> None:
        for gate in self.gates:
            gate.set()
        self._wait_for(
            lambda: all(
                thread is None
                for thread in (
                    self.window._worker_thread,
                    self.window._dp5_status_thread,
                )
            )
        )
        self.window._shutdown_requested = True
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def _gate(self) -> threading.Event:
        gate = threading.Event()
        self.gates.append(gate)
        return gate

    def _wait_for(self, predicate, timeout: float = 3.0) -> None:
        loop = QEventLoop()
        poll = QTimer()
        poll.setInterval(1)
        poll.timeout.connect(lambda: loop.quit() if predicate() else None)
        deadline = QTimer()
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        poll.start()
        deadline.start(int(timeout * 1000))
        if not predicate():
            loop.exec()
        poll.stop()
        deadline.stop()
        self.assertTrue(predicate(), "Qt event-loop condition did not complete")

    def _result(self, *, aborted: bool = False) -> ApplicationRunResult:
        return ApplicationRunResult(
            input_path=str(self.input_path),
            analysis_data={"total_found": 0, "aborted": aborted},
            report_data={"aborted": aborted},
            issue_counts={"info": 0, "warning": 0, "critical": 0, "anomaly_count": 0},
        )

    def test_partial_export_summary_retains_findings_and_disables_missing_artifacts(self):
        html = Path(self.temp) / 'findings.html'
        html.write_text('<p>Available findings</p>')
        result = self._result()
        result.report_data.update(partial=True, html_path=str(html),
            pdf_path=str(Path(self.temp) / 'missing.pdf'), output_dir=self.temp,
            artifact_warnings=['Findings PDF unavailable: converter error.'])
        dashboard = self.window.dashboard_page
        dashboard.show_summary(result, self.temp)
        self.assertIn('Partial export', dashboard.status_badge.text())
        self.assertTrue(dashboard.open_html_button.isEnabled())
        self.assertFalse(dashboard.open_pdf_button.isEnabled())
        self.assertTrue(dashboard.open_output_folder_button.isEnabled())
        self.assertIn('converter error', dashboard.summary_card.toolTip())
        result.analysis_data['aborted'] = True
        dashboard.show_summary(result, self.temp)
        self.assertIn('Cancelled', dashboard.status_badge.text())

    def test_terminal_messages_keep_assessment_export_and_cancellation_distinct(self):
        for status, partial, aborted, expected, assessment_label in [
            ("none", False, False, "Completed", "Nothing assessed"),
            ("partial", True, False, "Partial export", "Partly assessed"),
            ("complete", False, False, "Completed", "Assessed"),
            ("none", True, True, "Cancelled", "Nothing assessed"),
        ]:
            with self.subTest(status=status, partial=partial, aborted=aborted):
                result = self._result(aborted=aborted)
                result.analysis_data["assessment"] = {
                    "status": status, "assessed_descriptions": 0 if status == "none" else 1,
                    "unassessed": [],
                }
                result.report_data["partial"] = partial
                with patch.object(self.window, "_worker_thread", Mock()):
                    self.window._handle_worker_success(self.window._run_generation, result)
                self.assertEqual(self.window.dashboard_page.progress_label.text(), expected)
                self.assertIn(assessment_label, self.window.dashboard_page.assessment_badge.text())
                self.assertEqual(self.window.statusBar().currentMessage(),
                    "Analysis cancelled." if aborted else
                    "Analysis finished with a partial export." if partial else "Analysis finished.")

    def test_run_config_build_does_not_validate_runtime_on_main_thread(self) -> None:
        self.availability.reset_mock()
        with patch("checksi.dp5q.client.validate_asset_metadata") as metadata:
            built = self.window._build_run_config()
        self.assertTrue(built.dp5.enabled)
        self.availability.assert_not_called()
        metadata.assert_not_called()

    def _assert_requested_dp5_reaches_runner(self, readiness: str) -> None:
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        gate = self._gate()
        checking = threading.Event()

        def availability(_settings):
            checking.set()
            if readiness == "pending":
                gate.wait(3)
            if readiness == "unavailable":
                raise DP5RuntimeValidationError(["Missing bundled model asset."])
            return {"available": True, "model_id": "test-model", "score_version": "test-score"}

        self.availability.side_effect = availability
        theme = Mock()
        theme.load_preference.return_value = ThemePreference.SYSTEM
        self.window = CheckSIMainWindow(base_config=self.cfg, theme_controller=theme)
        self._wait_for(checking.is_set)
        if readiness != "pending":
            self._wait_for(lambda: self.window._dp5_status_thread is None)
        else:
            self.assertIsNotNone(self.window._dp5_status_thread)
        captured = []

        def execute(_runner, config, _input_path, *, cancel_token):
            captured.append(config.model_copy(deep=True))
            return self._result()

        with patch("checksi.gui.worker.ApplicationRunner.execute", execute):
            self.assertTrue(self.window.dashboard_page.run_button.isEnabled())
            self.window._start_analysis()
            self._wait_for(lambda: self.window._worker_thread is None)
        self.assertEqual(len(captured), 1)
        self.assertTrue(captured[0].dp5.enabled)
        self.assertEqual(captured[0], self.window._last_run_config)
        self.assertTrue(self.window.config_editor_page.dp5_settings().enabled)
        self.assertTrue(self.window.dashboard_page.dp5_enabled())
        self.assertTrue(self.window.dashboard_page.dp5_checkbox.isEnabled())
        if readiness == "pending":
            self.assertIsNotNone(self.window._dp5_status_thread)
        self.window.dashboard_page.dp5_checkbox.click()
        self.assertFalse(self.window.config_editor_page.dp5_settings().enabled)
        self.assertFalse(self.window.dashboard_page.dp5_enabled())
        with patch("checksi.gui.worker.ApplicationRunner.execute", execute):
            self.window._start_analysis()
            self._wait_for(lambda: self.window._worker_thread is None)
        self.assertEqual(len(captured), 2)
        self.assertFalse(captured[1].dp5.enabled)
        gate.set()

    def test_start_before_readiness_completes_preserves_requested_dp5(self) -> None:
        self._assert_requested_dp5_reaches_runner("pending")

    def test_start_with_unavailable_assets_preserves_requested_dp5(self) -> None:
        self._assert_requested_dp5_reaches_runner("unavailable")

    def test_start_with_available_assets_preserves_requested_dp5(self) -> None:
        self._assert_requested_dp5_reaches_runner("available")

    def test_unavailable_assets_are_status_messages_without_critical_tracebacks(self) -> None:
        self.availability.side_effect = DP5RuntimeValidationError(["Missing bundled model asset."])
        with patch.object(self.window.dashboard_page, "append_log",
                          wraps=self.window.dashboard_page.append_log) as append:
            self.window._refresh_dp5_runtime_status(show_message=True)
            self._wait_for(lambda: self.window._dp5_status_thread is None)
        self.assertFalse(self.window._dp5_runtime_valid)
        self.assertEqual(self.window._dp5_runtime_messages, ["Missing bundled model asset."])
        self.assertFalse(any(call.args[0] == "critical" for call in append.call_args_list))
        self.assertNotIn("Traceback", self.window.dashboard_page.log_output.toPlainText())
        self.assertEqual(self.window.statusBar().currentMessage(), "Bundled DP5q is unavailable.")

    def test_window_icon_loads_outside_the_repository_root(self) -> None:
        previous = os.getcwd()
        os.chdir(self.temp)
        try:
            theme = Mock()
            theme.load_preference.return_value = ThemePreference.SYSTEM
            window = CheckSIMainWindow(base_config=self.cfg, theme_controller=theme)
        finally:
            os.chdir(previous)
        try:
            self._wait_for(lambda: window._dp5_status_thread is None)
            self.assertFalse(window.windowIcon().isNull())
        finally:
            window.close()
            window.deleteLater()
            self.app.processEvents()

    def test_unrepresentable_dp5_values_are_kept_until_the_control_changes(self) -> None:
        editor = self.window.config_editor_page
        config = self.cfg.model_copy(deep=True)
        config.dp5.score_threshold_percent = 2.25
        config.dp5.timeout_seconds = 3600
        editor.load_from_config(config)
        self.assertEqual(editor.dp5_timeout_spinbox.value(), 1800)
        self.assertEqual(editor.dp5_settings().score_threshold_percent, 2.25)
        self.assertEqual(editor.dp5_settings().timeout_seconds, 3600)
        self.assertIn("3600", editor.dp5_timeout_spinbox.toolTip())
        self.assertEqual(editor.build_config().dp5.timeout_seconds, 3600)
        editor.dp5_timeout_spinbox.setValue(600)
        self.assertEqual(editor.dp5_settings().timeout_seconds, 600)
        self.assertEqual(editor.dp5_settings().score_threshold_percent, 2.25)
        config.dp5.score_threshold_percent = 12.5
        config.dp5.timeout_seconds = 900
        editor.load_from_config(config)
        self.assertEqual(editor.dp5_settings().score_threshold_percent, 12.5)
        self.assertEqual(editor.dp5_settings().timeout_seconds, 900)
        self.assertEqual(editor.dp5_timeout_spinbox.toolTip(), "")

    def test_editing_a_preserved_dp5_control_uses_the_edited_value(self) -> None:
        editor = self.window.config_editor_page
        config = self.cfg.model_copy(deep=True)
        config.dp5.score_threshold_percent = 2.25
        config.dp5.timeout_seconds = 3600
        editor.load_from_config(config)
        self.assertIn("more decimals", editor.dp5_threshold_spinbox.toolTip())
        self.assertIn("outside this control's range", editor.dp5_timeout_spinbox.toolTip())
        editor.dp5_timeout_spinbox.setValue(1500)
        editor.dp5_timeout_spinbox.setValue(1800)
        self.assertEqual(editor.dp5_settings().timeout_seconds, 1800)
        self.assertEqual(editor.dp5_timeout_spinbox.toolTip(), "")
        position = editor.dp5_threshold_slider.value()
        editor.dp5_threshold_slider.setValue(position + 1)
        editor.dp5_threshold_slider.setValue(position)
        self.assertEqual(editor.dp5_settings().score_threshold_percent, editor.dp5_threshold_spinbox.value())

    def test_out_of_range_dp5_timeout_loads_without_overflowing_the_control(self) -> None:
        editor = self.window.config_editor_page
        config = self.cfg.model_copy(deep=True)
        config.dp5.timeout_seconds = 2**31
        editor.load_from_config(config)
        self.assertEqual(editor.dp5_timeout_spinbox.value(), 1800)
        self.assertEqual(editor.dp5_settings().timeout_seconds, 2**31)

    def test_dp5_skip_reasons_are_listed_on_the_skipped_badge(self) -> None:
        dashboard = self.window.dashboard_page
        dashboard._render_dp5_summary({"n": 0, "skipped": 2, "failed": 0,
                                       "skipped_reasons": {"structure_unavailable_deep_name_analysis_off": 2}})
        self.assertEqual(dashboard.dp5_skipped_badge.toolTip(), "Skip reasons: structure_unavailable_deep_name_analysis_off=2")
        dashboard._render_dp5_summary({"n": 1, "skipped": 0, "failed": 0, "mean_score_percent": 50.0})
        self.assertEqual(dashboard.dp5_skipped_badge.toolTip(), "")

    def test_startup_configuration_notices_reach_the_visible_log(self) -> None:
        self.window.show_configuration_notices(("Legacy DP5 field migrated in memory.",))
        self.assertIn("Configuration notice: Legacy DP5 field migrated in memory.",
                      self.window.dashboard_page.log_output.toPlainText())

    def _legacy_config_file(self) -> Path:
        """Write a real migratable file without touching repository defaults."""
        from tests.test_dp5q_config import CONFIG
        path = Path(self.temp) / "legacy-default.toml"
        path.write_text(CONFIG + "probability_threshold_percent = 17.5\nmanaged = true\n")
        return path

    def test_unavailable_runtime_preserves_editor_and_file_default_requests(self) -> None:
        self.availability.side_effect = DP5RuntimeValidationError(["Missing bundled model asset."])
        self.window._refresh_dp5_runtime_status(show_message=False)
        self._wait_for(lambda: self.window._dp5_status_thread is None)
        self.window.dashboard_page.dp5_checkbox.setChecked(False)
        self.window.dashboard_page.dp5_checkbox.click()
        self.assertTrue(self.window.dashboard_page.dp5_enabled())
        self.window._handle_default_config_restore(self.cfg)
        self.assertTrue(self.window.dashboard_page.dp5_enabled())
        self._wait_for(lambda: self.window._dp5_status_thread is None)
        self.assertTrue(self.window._build_run_config().dp5.enabled)
        self.assertEqual(self.window._build_run_config().modes.deep_name_analysis.value, "off")

    def test_factory_restore_does_not_read_or_write_user_settings(self) -> None:
        path = self._legacy_config_file()
        original = path.read_bytes()
        with patch("checksi.gui.config_editor.user_config_path", return_value=path):
            self.window.config_editor_page._restore_default_config()
        self._wait_for(lambda: self.window._dp5_status_thread is None)
        text = self.window.dashboard_page.log_output.toPlainText()
        self.assertNotIn("Configuration notice:", text)
        self.assertEqual(self.window.config_editor_page.build_config().dp5.score_threshold_percent, 20.0)
        self.assertEqual(path.read_bytes(), original)

    def test_application_bootstrap_delivers_real_load_notices_to_the_new_window(self) -> None:
        from checksi.gui import app as bootstrap
        path = self._legacy_config_file()
        original = path.read_bytes()
        windows = []
        def create_window(**kwargs):
            window = CheckSIMainWindow(**kwargs)
            windows.append(window)
            return window
        application = Mock(wraps=self.app)
        application.exec.return_value = 0
        theme = Mock()
        theme.load_preference.return_value = ThemePreference.SYSTEM
        with patch.object(bootstrap, "QApplication", return_value=application), \
             patch.object(bootstrap, "ThemeController", return_value=theme), \
             patch.object(bootstrap, "ConfigManager", side_effect=lambda: ConfigManager(path)), \
             patch.object(bootstrap, "CheckSIMainWindow", side_effect=create_window):
            self.assertEqual(bootstrap.main(["checksi-gui"]), 0)
        self.assertEqual(len(windows), 1)
        window = windows[0]
        self._wait_for(lambda: window._dp5_status_thread is None)
        text = window.dashboard_page.log_output.toPlainText()
        self.assertIn("Configuration notice: dp5.probability_threshold_percent is deprecated;", text)
        self.assertIn("Legacy DP5 runtime settings are no longer used: managed", text)
        self.assertEqual(path.read_bytes(), original)
        window.close()
        window.deleteLater()
        self.app.processEvents()

    def test_conflicting_threshold_file_shows_both_keys_in_gui_error(self) -> None:
        path = self._legacy_config_file()
        path.write_text(path.read_text() + "score_threshold_percent = 12\n")
        with patch("checksi.gui.config_editor.QFileDialog.getOpenFileName", return_value=(str(path), "")), \
             patch("checksi.gui.config_editor.QMessageBox.warning") as warning:
            self.window.config_editor_page._load_config_from_file()
        message = warning.call_args.args[2]
        self.assertIn("Conflicting DP5 threshold settings", message)
        self.assertIn("probability_threshold_percent", message)
        self.assertIn("score_threshold_percent", message)

    def test_legacy_file_load_notices_reach_the_visible_log_without_writing(self) -> None:
        path = Path(self.temp) / "legacy.toml"
        values = self.cfg.model_dump(mode="json")
        values["solvents"]["entries"] = [{"name": "CDCl3", "aliases": [],
                                          "proton_shifts": [7.28], "carbon_shifts": [77.0]}]
        ConfigManager(path).save(AppConfig.model_validate(values))
        original = path.read_text().replace("score_threshold_percent", "probability_threshold_percent")
        path.write_text(original)
        original_bytes = path.read_bytes()
        with patch("checksi.gui.config_editor.QFileDialog.getOpenFileName",
                   return_value=(str(path), "TOML Files (*.toml)")):
            self.window.config_editor_page._load_config_from_file()
        self._wait_for(lambda: self.window._dp5_status_thread is None)
        self.assertIn("Configuration notice: dp5.probability_threshold_percent is deprecated;",
                      self.window.dashboard_page.log_output.toPlainText())
        self.assertEqual(path.read_bytes(), original_bytes)

    def test_legacy_advanced_apply_notices_reach_log_and_status_without_writing(self) -> None:
        from checksi.gui.config_editor import AdvancedConfigDialog

        path = Path(self.temp) / "current-settings.toml"
        manager = ConfigManager(path)
        manager.save(self.cfg)
        original = path.read_bytes()
        self.window.config_editor_page.set_config_source(manager)
        values = self.cfg.model_dump(mode="json")
        del values["dp5"]["score_threshold_percent"]
        values["dp5"].update(
            probability_threshold_percent=17,
            managed=True,
            install_root="/legacy/runtime",
            expected_rev="legacy-revision",
        )

        def apply(dialog):
            dialog.session_edit.setPlainText(json.dumps(values))
            dialog._apply_session()
            return dialog.result()

        with warnings.catch_warnings(record=True) as captured, \
             patch.object(AdvancedConfigDialog, "exec", new=apply), \
             patch.object(ConfigManager, "save") as save:
            warnings.simplefilter("always")
            self.window.config_editor_page._show_advanced_configuration()
        self._wait_for(lambda: self.window._dp5_status_thread is None)
        save.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(captured, [])
        config = self.window.config_editor_page.build_config()
        self.assertEqual(config.dp5.score_threshold_percent, 17)
        self.assertNotIn("install_root", config.dp5.model_dump())
        text = self.window.dashboard_page.log_output.toPlainText()
        self.assertIn("Configuration notice: dp5.probability_threshold_percent is deprecated;", text)
        self.assertIn("Legacy DP5 runtime settings are no longer used: expected_rev, install_root, managed", text)
        self.assertTrue(self.window.statusBar().currentMessage().startswith(
            "Configuration notice: Legacy DP5 runtime settings are no longer used:"
        ))

    def test_worker_captures_config_before_background_execution(self) -> None:
        worker = AnalysisWorker(
            config=self.cfg,
            input_path=str(self.input_path),
            event_dispatcher=lambda _event: None,
            cancel_token=threading.Event(),
        )
        self.cfg.dp5.enabled = False
        with patch("checksi.gui.worker.ApplicationRunner") as runner:
            worker.run()
        self.assertTrue(runner.return_value.execute.call_args.args[0].dp5.enabled)

    def test_runner_construction_failure_releases_worker_with_actionable_preflight_error(self) -> None:
        worker = AnalysisWorker(
            config=self.cfg,
            input_path=str(self.input_path),
            event_dispatcher=lambda _event: None,
            cancel_token=threading.Event(),
        )
        failures = []
        completed = []
        worker.failed.connect(lambda _operation_id, failure: failures.append(failure))
        worker.done.connect(lambda: completed.append(True))
        error = DP5RuntimeValidationError(["Missing bundled assets"])
        with patch("checksi.gui.worker.ApplicationRunner", side_effect=error):
            worker.run()
        self.assertEqual(completed, [True])
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].kind, "dp5_preflight")
        self.assertIn("Refresh Status", failures[0].message)
        self.assertIn("Missing bundled assets", failures[0].message)

    def test_event_bridge_preserves_typed_phase_and_detail_payloads(self) -> None:
        bridge = QtEventBridge(operation_id=23)
        events = []
        progress = []
        bridge.event_received.connect(events.append)
        bridge.progress_received.connect(lambda *args: progress.append(args))
        phase = ProgressEvent(0, 0, "Preflight")
        detail = ProgressEvent(2, 7, "Analyzing paragraph 2 of 7")
        bridge.dispatch(phase)
        bridge.dispatch(detail)
        self.assertEqual(events, [phase, detail])
        self.assertEqual(progress, [(23, 0, 0, "Preflight"), (23, 2, 7, detail.message)])

    def test_success_keeps_ownership_until_matching_thread_finishes(self) -> None:
        entered = self._gate()
        release = self._gate()
        result = self._result()

        def execute(_config, _path, *, cancel_token):
            entered.set()
            release.wait(5)
            return result

        with patch("checksi.gui.worker.ApplicationRunner.execute", side_effect=execute):
            self.window._start_analysis()
            self._wait_for(entered.is_set)
            thread = self.window._worker_thread
            worker = self.window._worker
            token = self.window._cancel_token
            # Hold only the thread event loop open after the real worker returns.
            worker.done.disconnect(thread.quit)
            try:
                release.set()
                self._wait_for(lambda: self.window._last_result is result)
                self.window._start_analysis()
                self.assertIs(self.window._worker_thread, thread)
                self.assertIs(self.window._worker, worker)
                self.assertIs(self.window._cancel_token, token)
                self.assertIs(self.window._run_state, RunState.RUNNING)
                self.assertFalse(self.window.dashboard_page.run_button.isEnabled())
                self.assertEqual(self.window.dashboard_page.progress_bar.maximum(), 1)
            finally:
                release.set()
                thread.quit()
            self._wait_for(lambda: self.window._worker_thread is None)
        self.assertIs(self.window._run_state, RunState.FINISHED)

    def test_queued_old_notifications_cannot_reset_a_new_run(self) -> None:
        entered = self._gate()
        release = self._gate()

        def execute(_config, _path, *, cancel_token):
            entered.set()
            release.wait(5)
            return self._result(aborted=cancel_token.is_set())

        self.window._run_generation = 8
        with patch("checksi.gui.worker.ApplicationRunner.execute", side_effect=execute):
            self.window._start_analysis()
            self._wait_for(entered.is_set)
            active_thread = self.window._worker_thread
            active_token = self.window._cancel_token
            bridge = QtEventBridge(operation_id=8)
            bridge.progress_received.connect(
                self.window._handle_progress_event, Qt.ConnectionType.QueuedConnection
            )
            bridge.log_received.connect(
                self.window._handle_log_event, Qt.ConnectionType.QueuedConnection
            )
            old_worker = AnalysisWorker(
                config=self.cfg,
                input_path=str(self.input_path),
                event_dispatcher=bridge.dispatch,
                cancel_token=threading.Event(),
                operation_id=8,
            )
            old_worker.succeeded.connect(
                self.window._handle_worker_success, Qt.ConnectionType.QueuedConnection
            )
            old_worker.failed.connect(
                self.window._handle_worker_failure, Qt.ConnectionType.QueuedConnection
            )
            old_thread = QThread()
            old_thread.finished.connect(
                self.window._handle_worker_finished, Qt.ConnectionType.QueuedConnection
            )
            self.window.dashboard_page.set_progress(0, 0, "Preflight")
            bridge.dispatch(ProgressEvent(1, 1, "Old progress"))
            bridge.dispatch(LogEvent(ErrorSeverity.INFO, "Old log"))
            old_worker.succeeded.emit(8, self._result())
            old_worker.failed.emit(8, WorkerFailure("Old failure", "Old details"))
            old_thread.finished.emit()
            delivered = []
            QTimer.singleShot(0, lambda: delivered.append(True))
            self._wait_for(lambda: bool(delivered))
            self.assertIs(self.window._worker_thread, active_thread)
            self.assertIs(self.window._cancel_token, active_token)
            self.assertIs(self.window._run_state, RunState.RUNNING)
            self.assertIsNone(self.window._last_result)
            self.assertEqual(self.window.dashboard_page.progress_label.text(), "Preflight")
            self.assertNotIn("Old failure", self.window.dashboard_page.log_output.toPlainText())
            self.assertNotIn("Old log", self.window.dashboard_page.log_output.toPlainText())
            release.set()
            self._wait_for(lambda: self.window._worker_thread is None)

    def _eligible_analysis(self):
        """Readiness now follows actual eligible non-boron analysis."""
        return self.enterContext(patch('checksi.application_runner.CheckSIController.run_analysis',
            return_value={'total_found': 1, 'error_stats': {}, 'aborted': False, 'compounds': [{
                'id': 1, 'smiles': 'CCO', 'run_dp5': True,
                'dp5_carbon_groups': [], 'dp5_c_signals_list': [60., 20.]}]}))

    def test_blocked_readiness_keeps_loop_responsive_rejects_overlap_and_defers_close(self) -> None:
        from checksi.document_parser import docx_paragraphs_and_omissions
        analysis = self._eligible_analysis()
        entered = self._gate()
        release = self._gate()
        captured = []

        def preflight(settings):
            captured.append((settings, threading.get_ident()))
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test preflight gate timed out")
            return object()

        ticks = []
        timer = QTimer()
        timer.setInterval(1)
        timer.timeout.connect(lambda: ticks.append(True))
        timer.start()
        self.addCleanup(timer.stop)
        with (
            patch("checksi.application_runner.check_availability", side_effect=preflight) as validate,
            patch("checksi.document_parser.docx_paragraphs_and_omissions",
                  wraps=docx_paragraphs_and_omissions) as extraction,
            patch("checksi.application_runner.enrich_analysis_with_dp5") as enrich,
            patch("checksi.application_runner.export_reports") as export,
            patch("checksi.application_runner.open_system_file") as open_file,
        ):
            self.window._start_analysis()
            self._wait_for(entered.is_set)
            original_thread = self.window._worker_thread
            original_token = self.window._cancel_token
            self.window._start_analysis()
            status_calls = self.availability.call_count
            self.window._refresh_dp5_runtime_status(show_message=False)
            self.assertIsNone(self.window._dp5_status_thread)
            self.assertEqual(self.availability.call_count, status_calls)
            # Timer ticks can predate the queued readiness-phase update.
            self._wait_for(
                lambda: len(ticks) >= 3
                and self.window.dashboard_page.progress_label.text() == "DP5 enrichment"
            )
            self.assertEqual(validate.call_count, 1)
            self.assertIs(self.window._worker_thread, original_thread)
            self.assertIsNone(self.window._dp5_status_thread)
            self.assertNotEqual(captured[0][1], threading.get_ident())
            self.assertTrue(captured[0][0].enabled)
            self.assertEqual(self.window.dashboard_page.progress_label.text(), "DP5 enrichment")
            close = QCloseEvent()
            self.window.closeEvent(close)
            self.assertFalse(close.isAccepted())
            self.assertTrue(original_token.is_set())
            self.assertIs(self.window._cancel_token, original_token)
            release.set()
            self._wait_for(lambda: self.window._worker_thread is None)
            extraction.assert_called_once()
            analysis.assert_called_once()
            self.convert.assert_not_called()
            enrich.assert_not_called()
            export.assert_not_called()
            open_file.assert_not_called()
        self.assertIsNone(self.window._worker)
        self.assertIsNone(self.window._cancel_token)
        self.assertTrue(self.window._last_result.analysis_data["aborted"])

    def test_failed_readiness_finishes_thread_and_returns_controls(self) -> None:
        analysis = self._eligible_analysis()
        doctor_calls = self.availability.call_count
        self.availability.side_effect = DP5RuntimeValidationError(["Missing bundled assets"])
        with patch(
            "checksi.application_runner.check_availability",
            side_effect=DP5RuntimeValidationError(["Missing bundled assets"]),
        ):
            self.window._start_analysis()
            self._wait_for(
                lambda: self.window._worker_thread is None
                and self.window._dp5_status_thread is None
            )
        self.assertIsNone(self.window._worker)
        analysis.assert_called_once()
        self.assertIsNone(self.window._cancel_token)
        self.assertIs(self.window._run_state, RunState.IDLE)
        self.assertTrue(self.window.dashboard_page.run_button.isEnabled())
        self.assertEqual(self.window.dashboard_page.progress_bar.maximum(), 1)
        self.assertIn("Refresh Status", self.window.dashboard_page.log_output.toPlainText())
        self.assertEqual(self.availability.call_count, doctor_calls + 1)
        self.assertFalse(self.window._dp5_runtime_valid)
        self.assertFalse(hasattr(self.window.config_editor_page, "dp5_install_button"))
        self.assertTrue(self.window.config_editor_page.dp5_refresh_button.isEnabled())

    def test_unrelated_run_failure_does_not_refresh_runtime_status(self) -> None:
        doctor_calls = self.availability.call_count
        with patch(
            "checksi.gui.worker.ApplicationRunner.execute",
            side_effect=RuntimeError("Document failed"),
        ):
            self.window._start_analysis()
            self._wait_for(lambda: self.window._worker_thread is None)
        self.assertEqual(self.availability.call_count, doctor_calls)
        self.assertTrue(self.window._dp5_runtime_valid)

    def test_close_waits_for_status_without_starting_pending_refresh(self) -> None:
        entered = self._gate()
        release = self._gate()

        def blocked(*_args, **_kwargs):
            entered.set()
            release.wait(5)
            return {"available": True}

        with patch("checksi.gui.worker.check_availability", side_effect=blocked) as work:
            self.window._refresh_dp5_runtime_status(show_message=False)
            self._wait_for(entered.is_set)
            self.window._refresh_dp5_runtime_status(show_message=True)
            close = QCloseEvent()
            self.window.closeEvent(close)
            self.assertFalse(close.isAccepted())
            self.assertTrue(self.window._shutdown_requested)
            release.set()
            self._wait_for(lambda: self.window._dp5_status_thread is None)
            self.assertEqual(work.call_count, 1)
            self.window.closeEvent(close)
            self.assertTrue(close.isAccepted())

    def test_bundled_status_has_no_install_or_reference_setup_controls(self) -> None:
        from checksi.gui import worker
        self.assertFalse(hasattr(worker, "DP5SetupWorker"))
        self.assertFalse(hasattr(self.window, "_start_dp5_setup"))
        self.assertFalse(hasattr(self.window.config_editor_page, "dp5_install_requested"))
        self.assertIn("model_id: test-model", self.window.config_editor_page.dp5_details_label.text())
        self.assertIn("score_version: test-score", self.window.config_editor_page.dp5_details_label.text())

    def test_accepted_close_prevents_a_queued_status_refresh(self) -> None:
        doctor_calls = self.availability.call_count
        delivered = []
        QTimer.singleShot(0, lambda: self.window._refresh_dp5_runtime_status(show_message=False))
        close = QCloseEvent()
        self.window.closeEvent(close)
        self.assertTrue(close.isAccepted())
        QTimer.singleShot(0, lambda: delivered.append(True))
        self._wait_for(lambda: bool(delivered))
        self.assertIsNone(self.window._dp5_status_thread)
        self.assertEqual(self.availability.call_count, doctor_calls)

    def _check_stale_status(self, *, failed: bool, pending_refresh: bool = False) -> None:
        entered = self._gate()
        release = self._gate()
        snapshots = []

        def doctor(settings):
            snapshots.append(settings.model_copy(deep=True))
            if len(snapshots) == 1:
                entered.set()
                release.wait(5)
                if failed:
                    raise RuntimeError("Old runtime failed")
                return {"available": False, "model_id": "old"}
            return {"available": True, "model_id": "latest"}

        self.availability.side_effect = doctor
        self.window._refresh_dp5_runtime_status(show_message=False)
        self._wait_for(entered.is_set)
        if pending_refresh:
            self.window._refresh_dp5_runtime_status(show_message=True)
        else:
            self.window._refresh_dp5_runtime_status(show_message=False)
        self.window.config_editor_page.set_dp5_enabled(True)
        self.window.dashboard_page.set_dp5_enabled(True)
        with patch.object(self.window.dashboard_page, "set_dp5_runtime_status", wraps=self.window.dashboard_page.set_dp5_runtime_status) as render:
            release.set()
            self._wait_for(lambda: len(snapshots) == 2 and self.window._dp5_status_thread is None)
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(render.call_args_list, [unittest.mock.call(True, ["Bundled DP5q assets are available.", "model_id: latest"])])
        self.assertTrue(self.window.dashboard_page.dp5_enabled())
        self.assertTrue(self.window.config_editor_page.dp5_settings().enabled)

    def test_stale_status_success_is_discarded_and_latest_settings_refreshed(self) -> None:
        self._check_stale_status(failed=False)

    def test_stale_status_failure_is_discarded_and_latest_settings_refreshed(self) -> None:
        self._check_stale_status(failed=True)

    def test_pending_refresh_supersedes_status_for_the_same_settings(self) -> None:
        self._check_stale_status(failed=False, pending_refresh=True)

    def test_non_runtime_edits_keep_the_inflight_status_and_confirmation(self) -> None:
        entered = self._gate()
        release = self._gate()
        doctor_calls = self.availability.call_count

        def doctor(_settings):
            entered.set()
            release.wait(5)
            return {"available": True}

        self.availability.side_effect = doctor
        self.window._refresh_dp5_runtime_status(show_message=True)
        self._wait_for(entered.is_set)
        editor = self.window.config_editor_page
        editor.dp5_threshold_spinbox.setValue(25)
        editor.dp5_enabled_checkbox.setChecked(False)
        editor.dp5_timeout_spinbox.setValue(37)
        editor.dp5_keep_artifacts_checkbox.setChecked(True)
        release.set()
        self._wait_for(lambda: self.window._dp5_status_thread is None)
        # Drain any queued replacement before checking that none was started.
        delivered = []
        QTimer.singleShot(0, lambda: delivered.append(True))
        self._wait_for(lambda: bool(delivered))
        self.assertEqual(self.availability.call_count, doctor_calls + 1)
        self.assertEqual(self.window.statusBar().currentMessage(), "Bundled DP5q is ready.")
        self.assertTrue(self.window.dashboard_page.dp5_enabled())
        self.assertTrue(editor.confirm_changes())
        self.assertFalse(self.window.dashboard_page.dp5_enabled())

    def test_explicit_refresh_confirmation_survives_superseding_status(self) -> None:
        entered = self._gate()
        release = self._gate()
        calls = []

        def availability(settings):
            calls.append(settings.model_copy(deep=True))
            if len(calls) == 1:
                entered.set()
                release.wait(5)
                return {"available": False}
            return {"available": True}

        self.availability.side_effect = availability
        self.window.statusBar().clearMessage()
        self.window._refresh_dp5_runtime_status(show_message=True)
        self._wait_for(entered.is_set)
        self.window._refresh_dp5_runtime_status(show_message=False)
        release.set()
        self._wait_for(lambda: len(calls) == 2 and self.window._dp5_status_thread is None)
        self.assertEqual(self.window.statusBar().currentMessage(), "Bundled DP5q is ready.")

    def test_active_run_defers_refresh_and_discards_an_inflight_status(self) -> None:
        status_entered, status_release = self._gate(), self._gate()
        run_entered, run_release = self._gate(), self._gate()
        calls = []

        def availability(_settings):
            calls.append(threading.get_ident())
            if len(calls) == 1:
                status_entered.set()
                status_release.wait(5)
                return {"available": False}
            return {"available": True}

        def execute(*_args, **_kwargs):
            run_entered.set()
            run_release.wait(5)
            return self._result()

        self.availability.side_effect = availability
        self.window._refresh_dp5_runtime_status(show_message=True)
        self._wait_for(status_entered.is_set)
        with patch("checksi.gui.worker.ApplicationRunner.execute", side_effect=execute):
            self.window._start_analysis()
            self._wait_for(run_entered.is_set)
            self.window._refresh_dp5_runtime_status(show_message=False)
            with patch.object(self.window.dashboard_page, "set_dp5_runtime_status") as render:
                status_release.set()
                self._wait_for(lambda: self.window._dp5_status_thread is None)
                render.assert_not_called()
                self.assertEqual(len(calls), 1)
                run_release.set()
                self._wait_for(lambda: self.window._worker_thread is None and self.window._dp5_status_thread is None and len(calls) == 2)
                render.assert_called_once_with(True, ["Bundled DP5q assets are available."])
        self.assertNotEqual(calls[0], threading.get_ident())
        self.assertEqual(self.window.statusBar().currentMessage(), "Bundled DP5q is ready.")

    def test_back_to_back_status_and_run_lifecycles_release_owners(self) -> None:
        with patch(
            "checksi.gui.worker.ApplicationRunner.execute", return_value=self._result()
        ) as execute:
            for cycle in range(200):
                with self.subTest(cycle=cycle):
                    self.window._refresh_dp5_runtime_status(show_message=False)
                    self._wait_for(lambda: self.window._dp5_status_thread is None)
                    self.assertIsNone(self.window._dp5_status_worker)
                    self.window._start_analysis()
                    self._wait_for(lambda: self.window._worker_thread is None)
                    self.assertIsNone(self.window._worker)
                    self.assertIsNone(self.window._cancel_token)
            self.assertEqual(execute.call_count, 200)
        self.convert.assert_not_called()

    def test_full_self_check_queues_behind_metadata_and_survives_another_refresh(self) -> None:
        entered, release = self._gate(), self._gate()
        doctor_threads = []

        def metadata(_settings):
            entered.set()
            release.wait(5)
            return {"available": True}

        def doctor(*_args, **_kwargs):
            doctor_threads.append(threading.get_ident())
            return 0, '{"ok": true, "messages": ["Full self-check passed."]}', ""

        self.availability.side_effect = metadata
        with patch("checksi.dp5q.client.check_availability", return_value={"model_id": "test-model"}), \
             patch("checksi.dp5q.client._run_diagnostics", side_effect=doctor):
            self.window._refresh_dp5_runtime_status(show_message=False)
            self._wait_for(entered.is_set)
            self.window.config_editor_page.dp5_self_check_button.click()
            self.window._refresh_dp5_runtime_status(show_message=False)
            release.set()
            self._wait_for(lambda: bool(doctor_threads) and self.window._dp5_status_thread is None)
        self.assertEqual(len(doctor_threads), 1)
        self.assertNotEqual(doctor_threads[0], threading.get_ident())
        self.assertEqual(self.window._dp5_runtime_messages, ["Owned DP5q ready: test-model", "Full self-check passed."])
        self.assertTrue(self.window.dashboard_page.dp5_enabled())
        self.assertIn("self-check passed", self.window.statusBar().currentMessage())

    def test_full_self_check_requested_during_analysis_is_deferred(self) -> None:
        entered, release = self._gate(), self._gate()

        def execute(*_args, **_kwargs):
            entered.set()
            release.wait(5)
            return self._result()

        with patch("checksi.dp5q.client.check_availability", return_value={"model_id": "test-model"}), \
             patch("checksi.dp5q.client._run_diagnostics", return_value=(0, '{"ok": true, "messages": ["Full check"]}', "")) as doctor, \
             patch("checksi.gui.worker.ApplicationRunner.execute", side_effect=execute):
            self.window._start_analysis()
            self._wait_for(entered.is_set)
            self.window.config_editor_page.dp5_self_check_button.click()
            self.window._refresh_dp5_runtime_status(show_message=False)
            doctor.assert_not_called()
            self.assertIn("queued", self.window.config_editor_page.dp5_status_message_label.text().lower())
            release.set()
            self._wait_for(lambda: doctor.call_count == 1 and self.window._worker_thread is None and self.window._dp5_status_thread is None)
        self.assertTrue(self.window.config_editor_page.dp5_settings().enabled)

    def test_close_waits_for_full_self_check_and_discards_queued_requests(self) -> None:
        entered, release = self._gate(), self._gate()

        def doctor(*_args, **_kwargs):
            entered.set()
            release.wait(5)
            return 0, '{"ok": true, "messages": ["Full check"]}', ""

        with patch("checksi.dp5q.client.check_availability", return_value={"model_id": "test-model"}), \
             patch("checksi.dp5q.client._run_diagnostics", side_effect=doctor) as full:
            self.window._refresh_dp5_runtime_status(show_message=True, full_check=True)
            self._wait_for(entered.is_set)
            self.window._refresh_dp5_runtime_status(show_message=True, full_check=True)
            close = QCloseEvent()
            self.window.closeEvent(close)
            self.assertFalse(close.isAccepted())
            release.set()
            self._wait_for(lambda: self.window._dp5_status_thread is None)
            self.assertEqual(full.call_count, 1)
            self.assertFalse(self.window._dp5_status_pending_full_check)

    def test_stale_full_self_check_repeats_against_latest_settings(self) -> None:
        entered, release = self._gate(), self._gate()
        snapshots = []

        def metadata(settings):
            snapshots.append(settings.model_copy(deep=True))
            return {"model_id": "test-model"}

        def doctor(*_args, **_kwargs):
            if len(snapshots) == 1:
                entered.set()
                release.wait(5)
                return 0, '{"ok": false, "messages": ["Stale full check"]}', ""
            return 0, '{"ok": true, "messages": ["Current full check"]}', ""

        with patch("checksi.dp5q.client.check_availability", side_effect=metadata), \
             patch("checksi.dp5q.client._run_diagnostics", side_effect=doctor):
            self.window._refresh_dp5_runtime_status(show_message=True, full_check=True)
            self._wait_for(entered.is_set)
            self.window.config_editor_page.dp5_timeout_spinbox.setValue(900)
            self.window._refresh_dp5_runtime_status(show_message=False)
            with patch.object(self.window.dashboard_page, "set_dp5_runtime_status", wraps=self.window.dashboard_page.set_dp5_runtime_status) as render:
                release.set()
                self._wait_for(lambda: len(snapshots) == 2 and self.window._dp5_status_thread is None)
            render.assert_called_once_with(True, ["Owned DP5q ready: test-model", "Current full check"])
        self.assertEqual(snapshots[1].timeout_seconds, 900)

    def test_analysis_is_refused_during_full_self_check_and_requires_retry(self) -> None:
        entered, release = self._gate(), self._gate()

        def doctor(*_args, **_kwargs):
            entered.set()
            release.wait(5)
            return 0, '{"ok": true, "messages": ["Full check"]}', ""

        with patch("checksi.dp5q.client.check_availability", return_value={"model_id": "test-model"}), \
             patch("checksi.dp5q.client._run_diagnostics", side_effect=doctor), \
             patch("checksi.gui.worker.ApplicationRunner.execute", return_value=self._result()) as execute:
            self.window._refresh_dp5_runtime_status(show_message=True, full_check=True)
            self._wait_for(entered.is_set)
            self.window._start_analysis()
            execute.assert_not_called()
            self.assertIsNone(self.window._worker_thread)
            self.assertIn("self-check", self.window.statusBar().currentMessage())
            release.set()
            self._wait_for(lambda: self.window._dp5_status_thread is None)
            self.app.processEvents()
            execute.assert_not_called()
            self.assertIsNone(self.window._worker_thread)
            self.window._start_analysis()
            self._wait_for(lambda: self.window._worker_thread is None)
            execute.assert_called_once()

    def test_phase_progress_shows_local_counts_without_a_global_percentage(self) -> None:
        dashboard = self.window.dashboard_page
        dashboard.set_progress(0, 0, "Preflight")
        self.assertEqual((dashboard.progress_bar.minimum(), dashboard.progress_bar.maximum()), (0, 0))
        dashboard.set_progress(2, 7, "Analysis")
        self.assertEqual(dashboard.progress_bar.value(), 2)
        self.assertEqual(dashboard.progress_bar.format(), "%v / %m")
        dashboard.set_progress(0, 0, "Report export")
        self.assertEqual(dashboard.progress_label.text(), "Report export")


if __name__ == "__main__":
    unittest.main()
