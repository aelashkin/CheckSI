"""CLI lifecycle behavior with real adapters and isolated external boundaries."""

from __future__ import annotations

import contextlib
import io
import importlib.util
import signal
import subprocess
import sys
import threading
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from checksi import main
from checksi.application_runner import ApplicationRunResult, ApplicationRunner
from checksi.config import ConfigManager
from checksi.dp5q.client import DP5qWorkerUnavailable
from checksi.config_schema import AppConfig, DeepNameAnalysis
from checksi.dp5q.client import DP5qAvailabilityError as DP5RuntimeValidationError
from checksi.events import LogEvent, ProgressEvent


class CLILifecycleTests(unittest.TestCase):
    """Run CLI orchestration without files, runtime installation or desktop opening."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.cfg = AppConfig.model_validate({
            "paths": {"input_path": "sample.pdf", "output_dir": self.root / "reports"},
            "modes": {"deep_name_analysis": "off", "output_mode": "detailed", "open_reports_on_success": False},
            "solvents": {"entries": []},
            "errors": {"default_severity": "warning"},
        })
        self.args = main._build_parser().parse_args(["run", "sample.pdf"])
        self.manager = ConfigManager(self.root / "settings.toml")
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.enterContext(contextlib.redirect_stderr(self.stderr))
        self.extract = self.enterContext(patch("checksi.document_parser.paragraphs_from_input", return_value=["General methods."]))
        self.export = self.enterContext(patch("checksi.application_runner.export_reports", return_value={
            "html_path": "actual.html", "pdf_path": "actual.pdf", "error_stats_path": "actual.xlsx",
            "highlighted_compounds": 0,
        }))

    def run_cli(self):
        self.manager.save(self.cfg)
        return main._handle_run(self.args, self.manager, log_callback=lambda message: print(message, file=self.stdout))

    def use_recognized_source(self, name='ethanol', smiles='CCO'):
        from tests.test_name_resolver import StubWorker
        self.cfg.modes.deep_name_analysis = DeepNameAnalysis.ON
        self.extract.return_value = [f'{name} (1)',
            '1H NMR (400 MHz, CDCl3): δ 1.20 (s, 5H). '
            '13C NMR (100 MHz, CDCl3): δ 30.0, 10.0.']
        self.enterContext(patch('checksi.analysis.name_resolver._ResolverWorker',
                                return_value=StubWorker(lambda backend, query: smiles)))

    def test_run_resolves_file_relative_output_before_invocation_overrides(self) -> None:
        self.cfg.paths.output_dir = Path("file-relative-reports")
        self.cfg.conversion.libreoffice_path = "tools/soffice"
        returned = ApplicationRunResult(
            input_path=str(Path.cwd() / "sample.pdf"), analysis_data={}, report_data={},
            issue_counts={"critical": 0, "warning": 0, "info": 0, "anomaly_count": 0},
        )
        with patch.object(ApplicationRunner, "execute", return_value=returned) as execute:
            self.assertEqual(self.run_cli(), 0)
        runtime = execute.call_args.args[0]
        self.assertEqual(runtime.paths.output_dir, self.root / "file-relative-reports")
        self.assertEqual(runtime.conversion.libreoffice_path, str(self.root / "tools/soffice"))
        self.assertEqual(runtime.paths.input_path, Path.cwd() / "sample.pdf")
        self.assertFalse(runtime.paths.output_dir.exists())

    def assert_summary_fields(self, *, assessment=False):
        summary = self.stdout.getvalue().split("=== Run Summary ===", 1)[1]
        fields = [line.split(":", 1)[0] for line in summary.splitlines() if ": " in line]
        expected = [
            "input_path", "compounds_detected", "critical_issues", "warning_issues", "informational_notes",
            "report_html", "report_pdf", "results_json", "error_stats_xlsx",
            "source_annotated_pdf", "output_dir", "export_status", "elapsed_seconds",
        ]
        if assessment:
            expected[5:5] = ["assessment_status", "assessed_descriptions", "unassessed_records"]
        self.assertEqual(fields, expected)

    def test_progress_visible_on_stderr_and_summary_includes_assessment(self) -> None:
        self.assertEqual(self.run_cli(), 3)
        self.assertIn("assessment_status: none", self.stdout.getvalue())
        self.assert_summary_fields(assessment=True)
        for phase in ["Preflight", "Document extraction", "Analysis", "Report export", "Run completed"]:
            self.assertIn(phase, self.stderr.getvalue())
        self.assertIn("Scanning paragraph 1/1", self.stderr.getvalue())
        self.assertNotIn("Progress:", self.stdout.getvalue())

    def test_anomalies_keep_exit_two(self) -> None:
        with patch("checksi.application_runner.CheckSIController.run_analysis", return_value={
            "total_found": 1, "error_stats": {"name_not_found": 1}, "aborted": False,
        }):
            self.assertEqual(self.run_cli(), 2)
        self.assert_summary_fields()

    def test_partial_export_takes_precedence_over_findings_and_reports_warning(self) -> None:
        self.export.return_value.update(partial=True, artifact_warnings=["Findings PDF unavailable."])
        with patch("checksi.application_runner.CheckSIController.run_analysis", return_value={
            "total_found": 1, "error_stats": {"name_not_found": 1}, "aborted": False,
        }):
            self.assertEqual(self.run_cli(), 3)
        self.assertIn("export_status: partial", self.stdout.getvalue())
        self.assertIn("artifact_warning: Findings PDF unavailable.", self.stdout.getvalue())

    def test_system_failure_keeps_exit_one_and_restores_handler(self) -> None:
        previous = signal.getsignal(signal.SIGINT)
        self.extract.side_effect = OSError("Unreadable input")
        self.assertEqual(self.run_cli(), 1)
        self.assertIs(signal.getsignal(signal.SIGINT), previous)
        self.assertIn("Unreadable input", self.stdout.getvalue())
        self.assertIn("Run failed", self.stderr.getvalue())

    def test_unconfirmed_worker_exit_during_close_is_a_concise_system_failure(self) -> None:
        self.cfg.dp5.enabled = True
        previous = signal.getsignal(signal.SIGINT)
        error = DP5qWorkerUnavailable("worker_cleanup_failed", "Worker process exit remains unconfirmed.")
        with patch("checksi.dp5_enrichment.DP5qSession") as session:
            session.return_value.__exit__.side_effect = error
            self.assertEqual(self.run_cli(), 1)
        self.export.assert_not_called()
        self.assertIn("worker_cleanup_failed", self.stdout.getvalue())
        self.assertNotIn("Traceback", self.stdout.getvalue())
        self.assertNotIn("Run Summary", self.stdout.getvalue())
        self.assertIs(signal.getsignal(signal.SIGINT), previous)

    def test_cancellation_exit_wins_over_an_unconfirmed_worker_close(self) -> None:
        self.cfg.dp5.enabled = True
        def cancelled_close(*_args):
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            raise DP5qWorkerUnavailable("worker_cleanup_failed", "Worker process exit remains unconfirmed.")
        with patch("checksi.dp5_enrichment.DP5qSession") as session:
            session.return_value.__exit__.side_effect = cancelled_close
            self.assertEqual(self.run_cli(), 130)
        self.export.assert_not_called()
        self.assertIn("worker_cleanup_failed", self.stdout.getvalue())
        self.assertIn("Run cancelled", self.stdout.getvalue())
        self.assertNotIn("Traceback", self.stdout.getvalue())

    def test_eligible_missing_assets_after_analysis_keep_guidance_exit_one_and_no_export(self) -> None:
        self.cfg.dp5.enabled = True
        self.use_recognized_source()
        with patch("checksi.application_runner.check_availability", side_effect=DP5RuntimeValidationError(["Missing bundled DP5q assets"])) as validate, \
             patch('checksi.dp5q.client.subprocess.Popen') as start:
            self.assertEqual(self.run_cli(), 1)
        validate.assert_called_once()
        # Applicability is known after analysis; eligible failures still precede export.
        self.extract.assert_called_once()
        self.export.assert_not_called()
        start.assert_not_called()
        self.assertIn("Missing bundled DP5q assets", self.stdout.getvalue())
        self.assertIn("dp5 doctor", self.stdout.getvalue())

    def test_boron_only_cli_exports_without_missing_model_failure_or_sidecar(self) -> None:
        from checksi.dp5q.observation_policy import BORON_REASON, BORON_WARNING
        self.cfg.dp5.enabled = True
        self.use_recognized_source('ethylboronic acid', 'CCB(O)O')
        with patch('checksi.application_runner.check_availability',
                   side_effect=DP5RuntimeValidationError(['Missing bundled DP5q assets'])) as ready, \
             patch('checksi.dp5q.client.check_availability', side_effect=AssertionError('readiness reached')), \
             patch('checksi.dp5q.client.subprocess.Popen') as start:
            self.assertNotEqual(self.run_cli(), 1)
        ready.assert_not_called()
        start.assert_not_called()
        self.export.assert_called_once()
        analysis = self.export.call_args.args[1]
        compound, = analysis['compounds']
        self.assertEqual(compound['dp5'], {'status': 'skipped', 'reason': BORON_REASON})
        self.assertEqual(analysis['dp5_summary']['failed'], 0)
        self.assertTrue(any(BORON_WARNING in warning for warning in compound['all_warnings']))
        self.assertNotIn('Missing bundled DP5q assets', self.stdout.getvalue())

    def test_sigint_before_analysis_returns_130_with_summary_and_restores_handler(self) -> None:
        previous = signal.getsignal(signal.SIGINT)
        def interrupt(*args, **kwargs):
            handler = signal.getsignal(signal.SIGINT)
            self.assertNotEqual(handler, previous)
            handler(signal.SIGINT, None)
            return ["General methods."]

        self.extract.side_effect = interrupt
        self.assertEqual(self.run_cli(), 130)
        self.assertIs(signal.getsignal(signal.SIGINT), previous)
        self.export.assert_not_called()
        self.assertIn("Cancellation requested", self.stderr.getvalue())
        self.assertIn("Run cancelled", self.stdout.getvalue())
        self.assertIn("report_html: not generated", self.stdout.getvalue())
        self.assert_summary_fields(assessment=True)

    def test_sigint_during_export_preserves_paths_and_overrides_anomaly_exit(self) -> None:
        artifacts = self.export.return_value.copy()
        def interrupt(*args, **kwargs):
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            return artifacts.copy()

        self.export.side_effect = interrupt
        with patch("checksi.application_runner.CheckSIController.run_analysis", return_value={
            "total_found": 1, "error_stats": {"name_not_found": 1}, "aborted": False,
        }):
            self.assertEqual(self.run_cli(), 130)
        self.assertIn("actual.html", self.stdout.getvalue())
        self.assertIn("actual.pdf", self.stdout.getvalue())
        self.assertIn("actual.xlsx", self.stdout.getvalue())
        self.assert_summary_fields()

    def test_sigint_handler_restored_even_on_unexpected_failure(self) -> None:
        previous = signal.getsignal(signal.SIGINT)
        self.extract.side_effect = TypeError("Unexpected failure")
        with self.assertRaises(TypeError):
            self.run_cli()
        self.assertIs(signal.getsignal(signal.SIGINT), previous)

    def test_closed_progress_stream_does_not_prevent_export_or_summary(self) -> None:
        class ClosedReader(io.StringIO):
            writes = 0

            def write(self, text):
                self.writes += 1
                raise BrokenPipeError("progress reader closed")

        broken = ClosedReader()
        with contextlib.redirect_stderr(broken):
            self.assertEqual(self.run_cli(), 3)
        self.assertEqual(broken.writes, 1)
        self.export.assert_called_once()
        self.assert_summary_fields(assessment=True)

    def test_non_tty_progress_keeps_phases_and_coarse_detail(self) -> None:
        progress = main._CLIProgressSink(threading.Event())
        progress(0, 0, "Analysis")
        for current in range(1, 1001):
            progress(current, 1000, f"Scanning paragraph {current}/1000")
        progress(0, 0, "Report export")
        lines = self.stderr.getvalue().splitlines()
        self.assertEqual(lines[0], "Progress: Analysis")
        self.assertIn("Progress: Scanning paragraph 1000/1000", lines)
        self.assertEqual(lines[-1], "Progress: Report export")
        self.assertLessEqual(len(lines), 14)

    def test_signal_handler_only_sets_token_without_buffered_io(self) -> None:
        token = threading.Event()
        with main._handle_run_sigint(token), patch.object(self.stderr, "write", side_effect=RuntimeError("reentrant write")) as write:
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        self.assertTrue(token.is_set())
        write.assert_not_called()

    def test_inherited_sigint_ignore_is_preserved(self) -> None:
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            with main._handle_run_sigint(threading.Event()):
                self.assertEqual(signal.getsignal(signal.SIGINT), signal.SIG_IGN)
            self.assertEqual(signal.getsignal(signal.SIGINT), signal.SIG_IGN)
        finally:
            signal.signal(signal.SIGINT, previous)

    def test_termination_and_hangup_cancel_cooperatively_and_restore_handlers(self) -> None:
        for name in ("SIGTERM", "SIGHUP"):
            if not hasattr(signal, name):
                continue
            signum = getattr(signal, name)
            with self.subTest(signal=name):
                self.stdout.seek(0)
                self.stdout.truncate()
                # The runner may inherit an ignored signal (nohup); the CLI
                # rightly keeps that, so start this case from the default.
                self.addCleanup(signal.signal, signum, signal.signal(signum, signal.SIG_DFL))
                previous = signal.getsignal(signum)

                def stop(*args, **kwargs):
                    signal.getsignal(signum)(signum, None)
                    return ["General methods."]

                self.extract.side_effect = stop
                self.assertEqual(self.run_cli(), 130)
                self.assertIs(signal.getsignal(signum), previous)
                self.assertIn("Run cancelled", self.stdout.getvalue())
                self.assert_summary_fields(assessment=True)

    @unittest.skipUnless(hasattr(signal, "SIGHUP"), "SIGHUP is POSIX-only")
    def test_inherited_sighup_ignore_is_preserved(self) -> None:
        previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
        try:
            with main._handle_run_sigint(threading.Event()):
                self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)
                self.assertNotEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
            self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)
        finally:
            signal.signal(signal.SIGHUP, previous)

    def test_configuration_load_errors_go_to_stderr(self) -> None:
        def broken():
            raise main.ConfigError("Invalid configuration data in settings.toml:\n- modes.header_margin: too large")

        manager = SimpleNamespace(load=broken)
        self.assertEqual(main._handle_config_view(manager, log_callback=lambda message: print(message, file=self.stdout)), 1)
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertIn("modes.header_margin", self.stderr.getvalue())

    def test_ctrl_c_outside_run_exits_130_without_traceback(self) -> None:
        with patch("checksi.main.ConfigManager") as manager, patch(
            "checksi.main.doctor_runtime", side_effect=KeyboardInterrupt,
        ):
            manager.return_value.load.return_value = self.cfg
            manager.return_value.migration_notices = ()
            self.assertEqual(main.main_cli(["dp5", "doctor"]), 130)
        self.assertIn("Cancelled.", self.stderr.getvalue())
        self.assertNotIn("Traceback", self.stderr.getvalue())

    def test_preflight_failure_after_cancel_returns_130_without_provisioning_hint(self) -> None:
        self.cfg.dp5.enabled = True
        self.use_recognized_source()

        def interrupt(settings):
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            raise DP5RuntimeValidationError(["Runtime check interrupted"])

        with patch("checksi.application_runner.check_availability", side_effect=interrupt):
            self.assertEqual(self.run_cli(), 130)
        self.assertIn("Run cancelled", self.stdout.getvalue())
        self.assertNotIn("scripts/setup.py", self.stdout.getvalue())
        self.extract.assert_called_once()
        self.export.assert_not_called()

    def test_cancel_during_unavailable_adapter_wins_over_original_readiness_failure(self) -> None:
        from checksi.dp5_enrichment import enrich_analysis_with_dp5
        self.cfg.dp5.enabled = True
        self.use_recognized_source()
        def interrupt(*args, **kwargs):
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            return enrich_analysis_with_dp5(*args, **kwargs)
        with patch('checksi.application_runner.check_availability',
                   side_effect=DP5RuntimeValidationError(['Missing bundled DP5q assets'])), \
             patch('checksi.application_runner.enrich_analysis_with_dp5', side_effect=interrupt), \
             patch('checksi.dp5q.client.subprocess.Popen') as start:
            self.assertEqual(self.run_cli(), 130)
        start.assert_not_called()
        self.export.assert_not_called()
        self.assertIn('Run cancelled', self.stdout.getvalue())
        self.assertNotIn('dp5 doctor', self.stdout.getvalue())

    def test_exception_after_cancellation_takes_precedence_over_system_exit(self) -> None:
        def interrupt(*args, **kwargs):
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            raise RuntimeError("Current operation interrupted")

        self.extract.side_effect = interrupt
        self.assertEqual(self.run_cli(), 130)
        self.assertIn("Run cancelled", self.stdout.getvalue())
        self.export.assert_not_called()

    def test_worker_cancellation_keeps_cancelled_run_summary(self) -> None:
        self.cfg.dp5.enabled = True
        analysis = {"total_found": 1, "error_stats": {}, "aborted": False, "compounds": [{
            "id": 1, "smiles": "CCO", "dp5_c_signals_list": [60.0, 20.0],
            "dp5_carbon_groups": [], "run_dp5": True,
        }]}
        def cancel_worker(_config, analysis_data, **_kwargs):
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            analysis_data["compounds"][0]["dp5"] = {"status": "cancelled"}

        with patch("checksi.application_runner.check_availability"), patch(
            "checksi.application_runner.CheckSIController.run_analysis", return_value=analysis,
        ), patch("checksi.application_runner.enrich_analysis_with_dp5", side_effect=cancel_worker):
            self.assertEqual(self.run_cli(), 130)
        self.assertIn("compounds_detected: 1", self.stdout.getvalue())
        self.assertIn("report_html: not generated", self.stdout.getvalue())
        self.assert_summary_fields()
        self.export.assert_not_called()

    def test_programmatic_cli_on_background_thread_does_not_install_signal_handler(self) -> None:
        results = []
        def run():
            try:
                results.append(self.run_cli())
            except Exception as exc:
                results.append(exc)

        thread = threading.Thread(target=run)
        with patch("checksi.main.signal.signal") as install:
            thread.start()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [3])
        install.assert_not_called()

    def test_cli_adapter_preserves_typed_event_payload(self) -> None:
        logs = []
        progress = []
        dispatch = main._build_event_handler(log_callback=logs.append, progress_callback=lambda *args: progress.append(args))
        dispatch(ProgressEvent(0, 0, "Preflight"))
        dispatch(ProgressEvent(3, 8, "Scanning paragraph 3/8"))
        from checksi.config_schema import ErrorSeverity
        dispatch(LogEvent(ErrorSeverity.INFO, "Analysis log"))
        self.assertEqual(progress, [(0, 0, "Preflight"), (3, 8, "Scanning paragraph 3/8")])
        self.assertEqual(logs, ["Analysis log"])


class SetupLifecycleTests(unittest.TestCase):
    """The retained setup flag checks bundled assets without a separate runtime install."""

    def setUp(self) -> None:
        path = Path(__file__).resolve().parents[1] / "scripts" / "setup.py"
        spec = importlib.util.spec_from_file_location("checksi_setup_test", path)
        self.setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.setup)
        self.sync = self.enterContext(patch.object(self.setup, "_run"))
        self.stdout = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.enterContext(patch.object(sys, "path", list(sys.path)))

    def test_plain_setup_syncs_only_the_locked_application_environment(self) -> None:
        with patch("checksi.dp5q.client.doctor_runtime") as doctor:
            self.assertEqual(self.setup.main([]), 0)
        self.sync.assert_called_once_with(["uv", "sync", "--locked"])
        doctor.assert_not_called()

    def test_failed_sync_names_the_product(self) -> None:
        self.sync.side_effect = subprocess.CalledProcessError(1, ["uv", "sync", "--locked"])
        self.assertEqual(self.setup.main([]), 1)
        self.assertIn("Setup failed while syncing CheckSI", self.stderr.getvalue())

    def test_with_dp5_performs_the_owned_self_check_and_reports_failure(self) -> None:
        for ok in (True, False):
            with self.subTest(ok=ok), patch(
                "checksi.dp5q.client.doctor_runtime", return_value=(ok, ["Model self-check result"]),
            ) as doctor:
                self.assertEqual(self.setup.main(["--with-dp5"]), 0 if ok else 1)
                doctor.assert_called_once_with()
        self.assertIn("Model self-check result", self.stdout.getvalue())
        self.assertIn("Repair or reinstall", self.stderr.getvalue())

    def test_cli_import_remains_headless(self) -> None:
        completed = subprocess.run([
            sys.executable, "-c",
            "import sys; from checksi import main; assert not any(name.startswith('PySide6') for name in sys.modules)",
        ], capture_output=True, text=True, timeout=40, check=False)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
