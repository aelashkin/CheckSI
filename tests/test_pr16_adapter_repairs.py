"""Refusals stay informational while genuine DP5q failures retain warnings."""

import unittest
from unittest.mock import patch

from checksi.dp5_enrichment import enrich_analysis_with_dp5
from checksi.dp5q.client import DP5qWorkerError
from checksi.dp5q.contracts import CheckResult
from checksi.events import LogEvent
from tests.test_dp5_review_fixes import review_config


class ApplicabilityReportingTests(unittest.TestCase):
    """Pin status, exact rendered text, severity, statistics and score absence."""

    def setUp(self):
        self.cfg = review_config()
        self.compound = {"id": 1, "smiles": "CCO", "run_dp5": True,
                         "dp5_c_signals_list": [100.0, 120.0], "dp5_carbon_groups": []}
        self.metadata = {"model_id": "fixture", "score_version": "dp5q-logtail-v1"}

    def run_result(self, result):
        with patch("checksi.dp5_enrichment.DP5qSession") as session:
            session.return_value.__enter__.return_value.check.return_value = result
            return enrich_analysis_with_dp5(self.cfg, {"compounds": [self.compound]},
                                            runtime_info=self.metadata)

    def test_real_same_side_predictions_are_scored(self):
        data = enrich_analysis_with_dp5(self.cfg, {"compounds": [self.compound]})
        self.assertEqual(self.compound['dp5']['status'], 'success')
        self.assertEqual(data["dp5_summary"]["skipped"], 0)
        self.assertEqual(data["dp5_summary"]["failed"], 0)
        self.assertEqual(data["error_stats"]["dp5_analysis_failed"], 0)
        self.assertEqual(data["error_stats"]["dp5_not_applicable"], 0)
        self.assertIn('score_raw', self.compound['dp5'])

    def test_all_worker_applicability_reasons_use_the_informational_identity(self):
        for reason in ("assignment_ambiguous", "unsupported_element", "no_converged_conformer",
                       "embedding_failed", "atom_limit", "isolated_atom", "neighbor_limit"):
            with self.subTest(reason=reason):
                self.compound.pop("all_warnings", None)
                message = "The complete structure is outside this method's supported domain."
                data = self.run_result(CheckResult(compound_id="1", status="not_applicable",
                                                    reason=reason, diagnostics={"message": message}))
                self.assertEqual(self.compound["all_warnings"],
                                 ["dp5_not_applicable. DP5q not applicable: " + message])
                self.assertEqual(data["error_stats"]["dp5_not_applicable"], 1)
                self.assertEqual(data["error_stats"]["dp5_analysis_failed"], 0)
                self.assertNotIn("score_raw", self.compound["dp5"])

    def test_mmff_sentence_is_rendered_once_without_duplicate_punctuation(self):
        message = "MMFF94s parameters are unavailable for fragment CB(O)O (atoms: C1, B2, O3, O4)."
        self.run_result(CheckResult(compound_id="1", status="not_applicable",
                                    reason="mmff_parameters_missing", diagnostics={"message": message}))
        self.assertEqual(self.compound["all_warnings"], [
            "dp5_not_applicable. DP5q not applicable: MMFF94s parameters are unavailable for "
            "fragment CB(O)O (atoms: C1, B2, O3, O4)."])

    def test_failed_status_remains_a_failure_even_with_an_applicability_reason(self):
        data = self.run_result(CheckResult(compound_id="1", status="failed",
                                           reason="mmff_parameters_missing",
                                           diagnostics={"message": "The calculation failed."}))
        self.assertEqual(data["dp5_summary"]["failed"], 1)
        self.assertEqual(data["dp5_summary"]["skipped"], 0)
        self.assertEqual(data["error_stats"]["dp5_analysis_failed"], 1)
        self.assertEqual(data["error_stats"]["dp5_not_applicable"], 0)
        self.assertEqual(self.compound["highlight_info"]["color"], "yellow")

    def test_worker_stderr_stays_in_local_logs_and_out_of_report_findings(self):
        tail = "Traceback: /private/example-user/model.py\n" * 1000
        failure = DP5qWorkerError("worker_exited", "The worker exited unexpectedly.", stderr_tail=tail)
        events = []
        with patch("checksi.dp5_enrichment.DP5qSession") as session:
            session.return_value.__enter__.return_value.check.side_effect = failure
            enrich_analysis_with_dp5(self.cfg, {"compounds": [self.compound]},
                                     runtime_info=self.metadata, event_dispatcher=events.append)
        self.assertLess(len(self.compound["dp5"]["error"]), 1200)
        self.assertNotIn("example-user", self.compound["dp5"]["error"])
        self.assertNotIn("example-user", "\n".join(self.compound["all_warnings"]))
        diagnostics = [event.message for event in events
                       if isinstance(event, LogEvent) and "example-user" in event.message]
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics, ["DP5q worker diagnostics (worker_exited):\n" + failure.stderr_tail])


if __name__ == "__main__":
    unittest.main()
