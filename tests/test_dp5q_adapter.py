"""Run-scoped adaptation, completed-result retention and artifact ownership."""

import json
import subprocess
import tempfile
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from checksi.config_schema import ErrorSeverity, DeepNameAnalysis
from checksi.dp5_enrichment import enrich_analysis_with_dp5
from checksi.dp5q.client import DP5qCancelled, DP5qWorkerUnavailable
from checksi.dp5q.client import DP5qSession as RealSession
from checksi.dp5q.contracts import CheckResult
from tests.test_dp5_review_fixes import review_config


class DP5qAdapterTests(unittest.TestCase):
    def setUp(self):
        self.cfg = review_config()
        self.metadata = {"model_id": "fixture", "score_version": "dp5q-logtail-v1"}
        self.compounds = [{"id": i, "smiles": "CCO", "run_dp5": True,
                           "dp5_c_signals_list": [18.23456789, 58.1], "dp5_carbon_groups": []}
                          for i in range(3)]

    def assessed(self, identity):
        return CheckResult(compound_id=identity, status="assessed", score_raw=0.5)

    def run_with(self, side_effect, *, token=None):
        with patch("checksi.dp5_enrichment.DP5qSession") as session:
            check = session.return_value.__enter__.return_value.check
            check.side_effect = side_effect
            result = enrich_analysis_with_dp5(self.cfg, {"compounds": self.compounds},
                                             runtime_info=self.metadata, cancel_token=token)
        return result, session, check

    def test_serial_compounds_share_one_session_and_preserve_precision(self):
        result, session, check = self.run_with(lambda request: self.assessed(request.compound_id))
        session.assert_called_once()
        self.assertEqual(check.call_count, 3)
        self.assertEqual(check.call_args_list[0].args[0].carbon_shifts[0], 18.23456789)
        self.assertEqual(check.call_args_list[0].args[0].carbon_labels, ("C1", "C2"))
        self.assertEqual(result["dp5_summary"]["successful"], 3)
        session.return_value.__exit__.assert_called_once()

    def test_real_compounds_share_one_worker_and_refused_input_stays_unscored(self):
        self.compounds[2]["dp5_c_signals_list"] = [18.2]
        with patch("checksi.dp5q.client.subprocess.Popen", wraps=subprocess.Popen) as start:
            result = enrich_analysis_with_dp5(self.cfg, {"compounds": self.compounds})
        self.assertEqual(start.call_count, 1)
        self.assertEqual(result["dp5_summary"]["successful"], 2)
        self.assertEqual(result["dp5_summary"]["failed"], 0)
        self.assertEqual(result["dp5_summary"]["skipped"], 1)
        self.assertEqual(self.compounds[0]["dp5"]["status"], "success")
        self.assertEqual(self.compounds[0]["dp5"]["score_raw"], self.compounds[1]["dp5"]["score_raw"])
        self.assertEqual(self.compounds[2]["dp5"]["reason"], "strict_nmr_coverage_mismatch")
        self.assertNotIn("score_raw", self.compounds[2]["dp5"])

    def test_terminal_failure_preserves_completed_score_and_marks_unassessed_remainder_failed(self):
        result, _, check = self.run_with([self.assessed("1"), DP5qWorkerUnavailable("worker_unavailable", "fixture")])
        self.assertEqual(check.call_count, 2)
        self.assertEqual(self.compounds[0]["dp5"]["score_percent"], 50)
        self.assertEqual(self.compounds[1]["dp5"]["status"], "failed")
        self.assertEqual(self.compounds[2]["dp5"]["reason"], "worker_unavailable")
        self.assertEqual(result["dp5_summary"]["successful"], 1)
        self.assertEqual(self.compounds[2]["dp5"]["status"], "failed")
        self.assertEqual(result["dp5_summary"]["failed"], 2)

    def test_active_cancellation_never_applies_a_partial_score(self):
        token = threading.Event()
        result, _, check = self.run_with([self.assessed("1"), DP5qCancelled()], token=token)
        self.assertEqual(check.call_count, 2)
        self.assertTrue(result["aborted"])
        self.assertTrue(token.is_set())
        self.assertEqual(self.compounds[0]["dp5"]["score_percent"], 50)
        for compound in self.compounds[1:]:
            self.assertEqual(compound["dp5"]["reason"], "cancelled")
            self.assertNotIn("score_raw", compound["dp5"])

    def test_low_score_warning_matches_its_threshold_comparison(self):
        self.cfg.dp5.score_threshold_percent = 10.
        def near_threshold(request):
            return CheckResult(compound_id=request.compound_id, status="assessed", score_raw=0.0999778)

        self.run_with(near_threshold)
        warning = next(line for line in self.compounds[0]["all_warnings"] if "dp5_low_probability." in line)
        self.assertIn("DP5q compatibility score is 9.998% (threshold 10.000%)", warning)

    def test_low_score_warning_keeps_a_true_bound_at_extreme_precision(self):
        for score_raw, threshold, expected in [
            (0.100000003, 10.0000004, "DP5q compatibility score is 10.0000003% (threshold 10.0000004%)"),
            (0.0, 1e-8, "DP5q compatibility score is 0% (threshold 1e-08%)"),
        ]:
            with self.subTest(threshold=threshold):
                self.setUp()
                self.cfg.dp5.score_threshold_percent = threshold
                self.run_with(lambda request, score_raw=score_raw: CheckResult(
                    compound_id=request.compound_id, status="assessed", score_raw=score_raw))
                warning = next(line for line in self.compounds[0]["all_warnings"] if "dp5_low_probability." in line)
                self.assertIn(expected, warning)

    def test_disabled_deep_analysis_skips_are_explained_in_the_run_log(self):
        self.cfg.modes.deep_name_analysis = DeepNameAnalysis.OFF
        compounds = [{"id": i, "run_dp5": True, "dp5_c_signals_list": [18.2, 58.1]} for i in range(2)]
        events = []
        with patch("checksi.dp5_enrichment.DP5qSession") as session:
            result = enrich_analysis_with_dp5(self.cfg, {"compounds": compounds}, runtime_info=self.metadata,
                                             event_dispatcher=events.append)
        session.return_value.__enter__.return_value.check.assert_not_called()
        self.assertEqual(result["dp5_summary"]["skipped_reasons"], {"structure_unavailable_deep_name_analysis_off": 2})
        messages = [event.message for event in events]
        self.assertIn("DP5q skip reasons: structure_unavailable_deep_name_analysis_off=2.", messages)
        warning = next(event for event in events if "deep name analysis is off" in event.message)
        self.assertEqual(warning.severity, ErrorSeverity.WARNING)

    def test_disabled_and_no_eligible_runs_start_no_process(self):
        with patch("checksi.dp5q.client.subprocess.Popen") as start:
            self.cfg.dp5.enabled = False
            enrich_analysis_with_dp5(self.cfg, {"compounds": self.compounds})
            self.cfg.dp5.enabled = True
            enrich_analysis_with_dp5(self.cfg, {"compounds": [{"id": 1}]}, runtime_info=self.metadata)
        start.assert_not_called()

    def test_malformed_real_child_rows_fail_enrichment_without_adapter_keyerror(self):
        sessions = []
        def create_session(*args, **kwargs):
            fixture = Path(__file__).parent / "fixtures/dp5q/controlled_worker.py"
            session = RealSession(*args, **kwargs, _command=[sys.executable, "-u", str(fixture), "malformed_assignment"])
            sessions.append(session)
            return session
        ready = {"model_id": "fixture", "score_version": "score", "preprocessing_id": "graph",
                 "geometry_policy": "single", "weights_sha256": "a" * 64}
        with patch("checksi.dp5_enrichment.DP5qSession", side_effect=create_session), \
             patch("checksi.dp5q.client.check_availability", return_value=ready):
            result = enrich_analysis_with_dp5(self.cfg, {"compounds": self.compounds}, runtime_info=ready)
        self.assertEqual(result["dp5_summary"]["successful"], 0)
        self.assertEqual(result["dp5_summary"]["failed"], 3)
        self.assertTrue(all(compound["dp5"]["reason"] == "protocol_error" for compound in self.compounds))
        self.assertEqual(len(sessions[0]._processes), 3)
        self.assertTrue(all("score_raw" not in compound["dp5"] for compound in self.compounds))
        self.assertTrue(all(process.poll() is not None for process in sessions[0]._processes))

    def test_oversized_structure_is_refused_before_native_parsing(self):
        self.compounds = [{"id": 1, "smiles": "C" * 16385}]
        with patch("checksi.dp5_enrichment.Chem.MolFromSmiles", side_effect=AssertionError("must not parse")):
            result, _, check = self.run_with([])
        check.assert_not_called()
        self.assertEqual(result["compounds"][0]["dp5"]["reason"], "structure_input_limit")

    def test_artifacts_are_new_finite_json_and_never_replace_prior_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.cfg.paths.output_dir = Path(temporary)
            self.cfg.dp5.keep_artifacts = True
            from checksi.artifacts import reserve_run_directory
            run_directories = []
            for _ in range(2):
                self.cfg.paths.output_dir = reserve_run_directory(temporary, "input.pdf")
                run_directories.append(self.cfg.paths.output_dir)
                self.run_with(lambda request: self.assessed(request.compound_id))
            self.assertNotEqual(run_directories[0], run_directories[1])
            files = list(Path(temporary).glob("*/dp5_artifacts/compound_*/assessment.json"))
            self.assertEqual(len(files), 6)
            for path in files:
                content = json.loads(path.read_text())
                self.assertEqual(content["result"]["score_raw"], 0.5)
                self.assertTrue(content["request"]["include_geometry"])
            self.assertFalse(list(Path(temporary).rglob("*.p")))


if __name__ == "__main__":
    unittest.main()
