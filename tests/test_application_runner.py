"""Application cancellation tests with document and export side effects isolated."""

from __future__ import annotations

import os
import threading
import tempfile
from pathlib import Path
import unittest
from unittest.mock import call, patch

from checksi.application_runner import ApplicationRunResult, ApplicationRunner
from checksi.config_schema import AppConfig, ErrorSeverity
from checksi.events import ApplicationEvent, LogEvent, ProgressEvent
from checksi.report_service import empty_report_data


class ApplicationRunnerCancellationTests(unittest.TestCase):
    """Exercise the real runner and controller without external runtimes or files."""

    def setUp(self) -> None:
        self.cfg = AppConfig.model_validate(
            {
                "paths": {
                    "input_path": "sample.pdf",
                    "output_dir": self.enterContext(tempfile.TemporaryDirectory()),
                },
                "modes": {
                    "deep_name_analysis": "off",
                    "output_mode": "detailed",
                    "open_reports_on_success": True,
                },
                "solvents": {"entries": []},
                "errors": {"default_severity": "warning"},
                "dp5": {"enabled": True},
            }
        )
        self.cancel_token = threading.Event()
        self.events: list[ApplicationEvent] = []
        self.input_path = os.path.abspath("sample.pdf")
        self.enterContext(
            patch(
                "checksi.document_parser.paragraphs_from_input",
                return_value=["General experimental methods."],
            )
        )
        self.enrich = self.enterContext(
            patch("checksi.application_runner.enrich_analysis_with_dp5")
        )
        self.validate = self.enterContext(
            patch("checksi.application_runner.check_availability")
        )
        self.export = self.enterContext(
            patch(
                "checksi.application_runner.export_reports",
                return_value={
                    "html_path": "findings.html",
                    "pdf_path": "report.pdf",
                    "highlighted_compounds": 0,
                    "error_stats_path": "errors.xlsx",
                },
            )
        )
        self.open_file = self.enterContext(
            patch("checksi.application_runner.open_system_file")
        )

    def _assert_aborted_result(self, result: ApplicationRunResult) -> None:
        self.assertEqual(result.input_path, self.input_path)
        self.assertIs(result.analysis_data["aborted"], True)
        self.assertEqual(result.report_data, {**empty_report_data(), 'aborted': True})
        self.assertEqual(
            result.issue_counts,
            {"info": 0, "warning": 0, "critical": 0, "anomaly_count": 0},
        )
        self.export.assert_not_called()
        self.open_file.assert_not_called()
        self.assertIn(
            LogEvent(
                severity=ErrorSeverity.WARNING,
                message="Analysis aborted before completion. Skipping report export.",
            ),
            self.events,
        )

    def test_pre_cancelled_run_skips_enrichment_and_export(self) -> None:
        self.cancel_token.set()
        runner = ApplicationRunner(event_dispatcher=self.events.append)

        result = runner.execute(self.cfg, "sample.pdf", self.cancel_token)

        self._assert_aborted_result(result)
        self.assertEqual(result.analysis_data["processed_paragraphs"], 0)
        self.enrich.assert_not_called()

    def test_cancellation_after_last_analysis_check_skips_enrichment_and_export(self) -> None:
        def receive_event(event: ApplicationEvent) -> None:
            self.events.append(event)
            if isinstance(event, ProgressEvent) and event.total > 0 and event.current == event.total:
                self.cancel_token.set()

        runner = ApplicationRunner(event_dispatcher=receive_event)

        result = runner.execute(self.cfg, "sample.pdf", self.cancel_token)

        self._assert_aborted_result(result)
        self.assertEqual(result.analysis_data["processed_paragraphs"], 1)
        self.enrich.assert_not_called()

    def test_cancellation_during_enrichment_skips_export_and_opening(self) -> None:
        def cancel_during_enrichment(cfg, analysis_data, **kwargs) -> None:
            self.assertIs(kwargs["cancel_token"], self.cancel_token)
            self.assertIs(analysis_data["aborted"], False)
            self.cancel_token.set()

        self.enrich.side_effect = cancel_during_enrichment
        runner = ApplicationRunner(event_dispatcher=self.events.append)

        result = runner.execute(self.cfg, "sample.pdf", self.cancel_token)

        self._assert_aborted_result(result)
        self.enrich.assert_called_once()

    def test_abort_flag_from_enrichment_skips_export_without_cancel_token(self) -> None:
        def abort_during_enrichment(cfg, analysis_data, **kwargs) -> None:
            analysis_data["aborted"] = True

        self.enrich.side_effect = abort_during_enrichment
        runner = ApplicationRunner(event_dispatcher=self.events.append)

        result = runner.execute(self.cfg, "sample.pdf")

        self._assert_aborted_result(result)
        self.enrich.assert_called_once()

    def test_successful_enrichment_exports_and_opens_reports(self) -> None:
        runner = ApplicationRunner(event_dispatcher=self.events.append)

        # Readiness is required by an eligible compound, rather than by the flag alone.
        with patch('checksi.application_runner.CheckSIController.run_analysis', return_value={
                'aborted': False, 'total_found': 1, 'error_stats': {}, 'compounds': [{
                    'id': 1, 'smiles': 'CCO', 'run_dp5': True,
                    'dp5_c_signals_list': [10., 20.], 'dp5_carbon_groups': []}]}):
            result = runner.execute(self.cfg, "sample.pdf", self.cancel_token)

        self.assertIs(result.analysis_data["aborted"], False)
        self.assertIs(result.report_data["aborted"], False)
        snapshot = self.enrich.call_args.args[0]
        self.assertIsNot(snapshot, self.cfg)
        self.assertEqual(snapshot.paths.input_path, Path(self.input_path))
        self.assertEqual(snapshot.dp5, self.cfg.dp5)
        self.assertIs(self.enrich.call_args.kwargs['cancel_token'], self.cancel_token)
        self.assertIs(self.enrich.call_args.kwargs['event_dispatcher'], runner._dispatch)
        self.assertIs(self.enrich.call_args.kwargs['runtime_info'], self.validate.return_value)
        self.assertIs(self.export.call_args.args[0], snapshot)
        self.assertIs(self.export.call_args.args[1], result.analysis_data)
        self.assertEqual(self.export.call_args.kwargs['pdf_path'], self.input_path)
        self.assertEqual(self.export.call_args.kwargs['run_dir'], snapshot.paths.output_dir)
        export_log = self.export.call_args.kwargs['log_callback']
        self.assertIs(export_log.__self__, runner)
        self.assertIs(export_log.__func__, ApplicationRunner._legacy_log_callback)
        self.assertEqual(
            self.open_file.call_args_list,
            [
                call("report.pdf", log=runner._legacy_log_callback),
                call("findings.html", log=runner._legacy_log_callback),
                call("errors.xlsx", log=runner._legacy_log_callback),
            ],
        )


class BoronReadinessOrderingTests(unittest.TestCase):
    """Real core/adapter applicability decides whether model assets are needed."""

    def sources(self, *, mixed=False):
        from types import SimpleNamespace
        from checksi.config import ConfigManager
        from checksi.config_schema import DeepNameAnalysis
        from checksi.document_parser import ParsedDocument
        from tests.test_name_resolver import StubWorker
        from unittest.mock import Mock
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        source = str(directory / 'input.pdf')
        proton = '1H NMR (400 MHz, CDCl3): δ 1.20 (s, 5H). '
        carbon = '13C NMR (100 MHz, CDCl3): δ 30.0, 10.0.'
        parts = ['ethylboronic acid (1)', proton + carbon]
        if mixed:
            parts.extend(['ethanol (2)', proton + carbon])
        cfg = ConfigManager.load_factory()
        cfg.paths.output_dir = directory
        cfg.modes.deep_name_analysis = DeepNameAnalysis.ON
        cfg.modes.open_reports_on_success = False
        cfg.dp5.enabled = True
        parser = SimpleNamespace(parse=Mock(return_value=ParsedDocument(source, parts, source)))
        worker = StubWorker(lambda backend, name: {'ethylboronic acid': 'CCB(O)O', 'ethanol': 'CCO'}.get(name))
        self.enterContext(patch('checksi.analysis.name_resolver._ResolverWorker', return_value=worker))
        return cfg, source, parser

    def test_boron_only_run_exports_without_model_readiness_or_sidecar(self):
        from checksi.dp5q.client import DP5qAvailabilityError
        from checksi.dp5q.observation_policy import BORON_REASON, BORON_WARNING
        from checksi.dp5_enrichment import _compound_request
        cfg, source, parser = self.sources()
        with patch('checksi.application_runner.check_availability',
                   side_effect=DP5qAvailabilityError(['Missing controlled model assets'])) as ready, \
             patch('checksi.dp5q.client.check_availability', side_effect=AssertionError('model readiness reached')), \
             patch('checksi.dp5_enrichment._compound_request', wraps=_compound_request) as prepare, \
             patch('checksi.dp5q.client.subprocess.Popen') as start, \
             patch('checksi.application_runner.export_reports', return_value={**empty_report_data(),
                   'export_status': 'complete', 'html_path': 'findings.html'}) as export:
            result = ApplicationRunner(document_parser=parser).execute(cfg, source)
        ready.assert_not_called()
        prepare.assert_called_once()
        start.assert_not_called()
        export.assert_called_once()
        compound, = result.analysis_data['compounds']
        self.assertEqual(compound['dp5']['reason'], BORON_REASON)
        self.assertEqual(compound['dp5']['status'], 'skipped')
        self.assertEqual(result.analysis_data['dp5_summary']['failed'], 0)
        self.assertEqual(sum(BORON_WARNING in warning for warning in compound['all_warnings']), 1)

    def test_eligible_missing_assets_keep_failure_before_export_and_mixed_boron_skips(self):
        from checksi.dp5q.client import DP5qAvailabilityError
        from checksi.dp5q.observation_policy import BORON_REASON
        from checksi.dp5_enrichment import enrich_analysis_with_dp5
        cfg, source, parser = self.sources(mixed=True)
        failure = DP5qAvailabilityError(['Missing controlled model assets'])
        with patch('checksi.application_runner.check_availability', side_effect=failure) as ready, \
             patch('checksi.application_runner.enrich_analysis_with_dp5', wraps=enrich_analysis_with_dp5) as enrich, \
             patch('checksi.dp5q.client.subprocess.Popen') as start, \
             patch('checksi.application_runner.export_reports') as export:
            with self.assertRaises(DP5qAvailabilityError) as raised:
                ApplicationRunner(document_parser=parser).execute(cfg, source)
        self.assertIs(raised.exception, failure)
        ready.assert_called_once()
        parser.parse.assert_called_once()
        start.assert_not_called()
        export.assert_not_called()
        enrich.assert_called_once()
        analysis = enrich.call_args.args[1]
        self.assertEqual(analysis['compounds'][0]['dp5'], {'status': 'skipped', 'reason': BORON_REASON})
        self.assertEqual(analysis['compounds'][1]['dp5']['status'], 'failed')
        self.assertEqual(analysis['dp5_summary']['skipped'], 1)
        self.assertEqual(analysis['dp5_summary']['failed'], 1)


if __name__ == "__main__":
    unittest.main()
