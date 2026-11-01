"""Shared preflight, phase progress and cancellation boundary tests."""

from __future__ import annotations

import os
import threading
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from checksi.application_runner import ApplicationRunner
from checksi.config_schema import AppConfig, ErrorSeverity
from checksi.dp5_enrichment import enrich_analysis_with_dp5
from checksi.dp5q.client import DP5qAvailabilityError as DP5RuntimeValidationError
from checksi.events import LogEvent, ProgressEvent


class RunLifecycleTests(unittest.TestCase):
    """Exercise real orchestration with external I/O replaced at its boundaries."""

    def setUp(self) -> None:
        self.cfg = AppConfig.model_validate({
            "paths": {"input_path": "sample.pdf", "output_dir": self.enterContext(tempfile.TemporaryDirectory())},
            "modes": {"deep_name_analysis": "off", "output_mode": "detailed", "open_reports_on_success": True},
            "solvents": {"entries": []},
            "errors": {"default_severity": "warning"},
            "dp5": {"enabled": True},
        })
        self.runtime = {"model_id": "fixture", "score_version": "dp5q-logtail-v1"}
        self.events = []
        self.token = threading.Event()
        self.validate = self.enterContext(patch("checksi.application_runner.check_availability", return_value=self.runtime))
        self.extract = self.enterContext(patch("checksi.document_parser.paragraphs_from_input", return_value=["General methods."]))
        self.open_file = self.enterContext(patch("checksi.application_runner.open_system_file"))
        self.export = self.enterContext(patch("checksi.application_runner.export_reports", return_value={
            "html_path": "actual.html", "pdf_path": "actual.pdf",
            "highlighted_compounds": 0, "error_stats_path": "actual.xlsx",
        }))

    def run_application(self, dispatch=None):
        return ApplicationRunner(event_dispatcher=dispatch or self.events.append).execute(self.cfg, "./sample.pdf", self.token)

    def eligible_analysis(self):
        return self.enterContext(patch('checksi.application_runner.CheckSIController.run_analysis',
            return_value={'total_found': 1, 'error_stats': {}, 'aborted': False, 'compounds': [{
                'id': 1, 'smiles': 'CCO', 'run_dp5': True,
                'dp5_carbon_groups': [], 'dp5_c_signals_list': [60., 20.]}]}))

    def test_one_readiness_after_eligible_analysis_and_no_enrichment_revalidation(self) -> None:
        analysis = self.eligible_analysis()
        def extract(*args, **kwargs):
            self.validate.assert_not_called()
            return ['General methods.']
        self.extract.side_effect = extract
        with patch('checksi.application_runner.enrich_analysis_with_dp5') as enrich, \
             patch('checksi.dp5q.client.check_availability') as duplicate:
            result = self.run_application()
        self.validate.assert_called_once()
        analysis.assert_called_once()
        duplicate.assert_not_called()
        self.assertIs(enrich.call_args.kwargs['runtime_info'], self.runtime)
        self.assertEqual(len(result.analysis_data['compounds']), 1)
        self.assertFalse(result.analysis_data["aborted"])

    def test_no_eligible_compounds_bypass_readiness(self):
        result = self.run_application()
        self.validate.assert_not_called()
        self.assertEqual(result.analysis_data['dp5_summary']['total_compounds'], 0)

    def test_disabled_dp5_does_not_validate(self) -> None:
        self.cfg.dp5.enabled = False
        self.run_application()
        self.validate.assert_not_called()

    def test_direct_enrichment_retains_validation_fallback(self) -> None:
        data = {'compounds': [{'id': 1, 'smiles': 'CCO', 'run_dp5': True,
                 'dp5_c_signals_list': [60., 20.], 'dp5_carbon_groups': []}], 'error_stats': {}}
        with patch('checksi.dp5q.client.check_availability',
                   side_effect=DP5RuntimeValidationError(['Missing controlled assets'])) as validate, \
             patch('checksi.dp5q.client.subprocess.Popen') as start:
            result = enrich_analysis_with_dp5(self.cfg, data)
        validate.assert_called_once_with(self.cfg.dp5)
        start.assert_not_called()
        self.assertEqual(result['dp5_summary']['total_compounds'], 1)
        self.assertEqual(result['dp5_summary']['failed'], 1)
        self.assertEqual(data['compounds'][0]['dp5']['status'], 'failed')
        self.assertNotIn('score_raw', data['compounds'][0]['dp5'])

    def test_invalid_readiness_for_eligible_analysis_stops_before_export_and_reports_failure(self) -> None:
        self.eligible_analysis()
        self.validate.side_effect = DP5RuntimeValidationError(["Missing runtime executable"])
        with self.assertRaises(DP5RuntimeValidationError):
            self.run_application()
        self.extract.assert_called_once()
        self.export.assert_not_called()
        self.assertEqual(self.events[-1], ProgressEvent(0, 0, "Run failed"))

    def test_active_run_uses_deep_snapshot_despite_session_edits(self) -> None:
        self.eligible_analysis()
        def edit_session(settings):
            self.cfg.dp5.enabled = False
            self.cfg.modes.open_reports_on_success = False
            self.cfg.paths.output_dir = Path("different-output")
            self.cfg.errors.default_severity = "disabled"
            return self.runtime

        self.validate.side_effect = edit_session
        with patch("checksi.application_runner.enrich_analysis_with_dp5") as enrich:
            self.run_application()
        snapshot = self.export.call_args.args[0]
        self.assertIsNot(snapshot, self.cfg)
        self.assertTrue(snapshot.dp5.enabled)
        self.assertNotEqual(snapshot.paths.output_dir, Path("different-output"))
        self.assertTrue(snapshot.paths.output_dir.is_dir())
        enrich.assert_called_once()
        self.assertIs(enrich.call_args.kwargs["runtime_info"], self.runtime)
        self.assertEqual(self.open_file.call_count, 3)

    def test_pre_cancelled_run_skips_validation_and_extraction(self) -> None:
        self.token.set()
        result = self.run_application()
        self.validate.assert_not_called()
        self.extract.assert_not_called()
        self.export.assert_not_called()
        self.open_file.assert_not_called()
        self.assertEqual(result.input_path, os.path.abspath("sample.pdf"))
        self.assertTrue(result.analysis_data["aborted"])
        self.assertEqual(result.analysis_data["processed_paragraphs"], 0)
        self.assertIsNone(result.report_data["html_path"])
        self.assertIsNone(result.report_data["pdf_path"])
        self.assertIsNone(result.report_data["error_stats_path"])
        self.assertEqual(result.issue_counts["anomaly_count"], 0)
        self.assertEqual(self.events[-1], ProgressEvent(0, 0, "Run cancelled"))

    def test_cancellation_during_readiness_skips_enrichment_and_export(self) -> None:
        self.eligible_analysis()
        def cancel(settings):
            self.token.set()
            return self.runtime

        self.validate.side_effect = cancel
        result = self.run_application()
        self.extract.assert_called_once()
        self.export.assert_not_called()
        self.assertTrue(result.report_data["aborted"])

    def test_cancellation_after_extraction_skips_analysis_and_export(self) -> None:
        def cancel(*args, **kwargs):
            self.token.set()
            return ["General methods."]

        self.extract.side_effect = cancel
        with patch("checksi.application_runner.CheckSIController") as controller:
            result = self.run_application()
        controller.assert_not_called()
        self.export.assert_not_called()
        self.assertTrue(result.analysis_data["aborted"])
        self.assertEqual(result.analysis_data["total_paragraphs"], 1)

    def test_cancel_at_phase_transition_does_not_start_that_phase(self) -> None:
        for phase in ["Preflight", "Document extraction", "Analysis", "DP5 enrichment", "Report export"]:
            with self.subTest(phase=phase):
                self.token.clear()
                self.extract.reset_mock()
                self.export.reset_mock()
                def dispatch(event):
                    if isinstance(event, ProgressEvent) and event.message == phase:
                        self.token.set()

                result = self.run_application(dispatch)
                self.assertTrue(result.analysis_data["aborted"])
                self.export.assert_not_called()
                if phase in ["Preflight", "Document extraction"]:
                    self.extract.assert_not_called()

    def test_cancel_during_export_retains_returned_artifacts_and_suppresses_open(self) -> None:
        artifacts = self.export.return_value.copy()
        def cancel(*args, **kwargs):
            self.token.set()
            return artifacts.copy()

        self.export.side_effect = cancel
        result = self.run_application()
        self.assertEqual(result.report_data, {**artifacts, "aborted": True})
        self.assertTrue(result.analysis_data["aborted"])
        self.open_file.assert_not_called()
        self.assertIn(LogEvent(
            ErrorSeverity.WARNING,
            "Run cancelled during report export. Generated artifacts retained; automatic opening skipped.",
        ), self.events)

    def test_cancellation_during_first_open_skips_remaining_artifacts(self) -> None:
        self.open_file.side_effect = lambda *args, **kwargs: self.token.set()
        result = self.run_application()
        self.assertEqual(self.open_file.call_count, 1)
        self.assertEqual(self.open_file.call_args.args[0], "actual.pdf")
        self.assertTrue(result.analysis_data["aborted"])
        self.assertEqual(result.report_data["html_path"], "actual.html")

    def test_typed_and_legacy_callbacks_receive_equivalent_phase_and_detail_events(self) -> None:
        self.cfg.dp5.enabled = False
        self.run_application()
        legacy_progress = []
        ApplicationRunner(log_callback=lambda message: None, progress_callback=lambda *args: legacy_progress.append(args)).execute(self.cfg, "sample.pdf")
        typed = [(event.current, event.total, event.message) for event in self.events if isinstance(event, ProgressEvent)]
        self.assertEqual(legacy_progress, typed)
        phases = [message for current, total, message in typed if total == 0]
        self.assertEqual(phases, ["Preflight", "Document extraction", "Analysis", "Report export", "Run completed"])
        self.assertIn((1, 1, "Scanning paragraph 1/1"), typed)


if __name__ == "__main__":
    unittest.main()
