"""Actual DP5q adapter requests with only the process session replaced."""

from dataclasses import asdict
import unittest
from unittest.mock import patch


from checksi.analysis.nmr import check_nmr
from checksi.config_schema import AppConfig, ErrorSeverity
from checksi.dp5_enrichment import enrich_analysis_with_dp5
from checksi.dp5q.contracts import CheckResult
from checksi.errors import ErrorCode
from checksi.utils.error_handler import ErrorTracker


class DP5AdapterTests(unittest.TestCase):
    """Validate spectra, equivalence constraints and labels before scoring."""

    def setUp(self):
        self.cfg = AppConfig.model_validate({
            "paths": {"input_path": "input.pdf", "output_dir": "."},
            "modes": {"deep_name_analysis": "on", "output_mode": "essential"},
            "solvents": {"entries": [{"name": "CDCl3", "aliases": [], "proton_shifts": [7.26], "carbon_shifts": [77.0]}]},
            "errors": {"default_severity": "warning", "overrides": {}},
            "dp5": {"enabled": True},
        })
        self.runtime = {"model_id": "fixture", "score_version": "dp5q-logtail-v1"}

    def enrich(self, compound, probability=0.25):
        requests = []
        def check(request):
            requests.append(request)
            return CheckResult(compound_id=request.compound_id, status="assessed", score_raw=probability)
        with patch("checksi.dp5_enrichment.check_availability", return_value=self.runtime), patch("checksi.dp5_enrichment.DP5qSession") as session:
            session.return_value.__enter__.return_value.check.side_effect = check
            result = enrich_analysis_with_dp5(self.cfg, {"compounds": [compound], "error_stats": {}})
        return result, requests

    def parsed_compound(self, signals, smiles="CCCO", count=3, groups=None):
        observations = []
        result = check_nmr(self.cfg, ErrorTracker(), f"13C NMR (100 MHz, CDCl3): δ {signals}.", "13C", 1, count, 400, 0, 0, None, 0, 0, carbon_groups=groups or [], observations_out=observations)
        return {"id": 1, "smiles": smiles, "all_warnings": result[0], "dp5_c_signals_list": result[7], "dp5_carbon_groups": result[5], "run_dp5": result[6],
                "carbon_observations": [asdict(item) for item in observations]}

    def test_equivalent_accepted_multiplicity_forms_reach_engine_with_all_shifts(self):
        # The equivalent methyl atoms form one formal environment. An annotation
        # counts overlapping environments under Pavel's restored signal policy.
        for signals in ["207.1, 30.9 (1C)", "207.1, 30.9 (1 C)", "207.1, 30.9 (1С)"]:
            with self.subTest(signals=signals):
                compound = self.parsed_compound(signals, smiles="CC(=O)C", count=2, groups=[["C1", "C4"]])
                result, requests = self.enrich(compound)
                self.assertEqual(len(requests), 1)
                request = requests[0]
                self.assertEqual(request.carbon_shifts, (207.1, 30.9))
                self.assertEqual(request.smiles, "CC(=O)C")
                self.assertEqual(request.carbon_groups, (("C1", "C4"),))
                self.assertEqual(request.observation_atom_counts, (None, 1))
                self.assertEqual(result["compounds"][0]["dp5"]["probability_percent"], 25.0)

    def test_atom_multiplicity_cannot_certify_overlapping_formal_environments(self):
        for signals in ["207.1, 30.9 (2C)", "207.1, 30.9 (2 C)", "207.1, 30.9 (2С)"]:
            with self.subTest(signals=signals):
                compound = self.parsed_compound(signals, smiles="CC(=O)C", count=2, groups=[["C1", "C4"]])
                result, requests = self.enrich(compound)
                self.assertFalse(compound["run_dp5"])
                self.assertEqual(requests, [])
                self.assertEqual(result["compounds"][0]["dp5"]["status"], "skipped")
                self.assertNotIn("probability_percent", result["compounds"][0]["dp5"])

    def test_valid_equivalence_groups_cover_real_atom_labels(self):
        # Oxygen occupies atom 2, so carbon labels must use the molecule's atom indices.
        compound = self.parsed_compound("60.0", smiles="COC", count=1, groups=[["C1", "C3"]])
        result, requests = self.enrich(compound)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].carbon_groups, (("C1", "C3"),))
        self.assertEqual(result["dp5_summary"]["successful"], 1)

    def test_invalid_requests_cannot_reach_engine_or_receive_score(self):
        for shifts, groups, expected_reason, expected_message in [
            ([60.0, 20.0], [], 'strict_nmr_coverage_mismatch',
             'DP5q not performed: the number of 13C signals (2) does not match the expected number (3).'),
            ([60.0, 60.0, 20.0], [["C1", "C3"]], 'strict_nmr_coverage_mismatch',
             'DP5q not performed: the number of 13C signals (3) does not match the expected number (2).'),
            ([60.0, 20.0], [["C1", "C4"]], 'invalid_equivalence_groups', None),
            ([60.0, 20.0], [["C1", "C1"]], 'invalid_equivalence_groups', None),
            ([60.0], [["C1", "C2"], ["C2", "C3"]], 'invalid_equivalence_groups', None),
            ([60.0, 20.0], [["C1", "H5"]], 'invalid_equivalence_groups', None),
            ([60.0, 20.0], [["C1"]], 'invalid_equivalence_groups', None),
            ([60.0, 20.0], ["C1,C3"], 'invalid_equivalence_groups', None),
            ([60.0, float("nan"), 20.0], [], 'invalid_request', None),
            ([60.0, float("inf"), 20.0], [], 'invalid_request', None),
            ([60.0, "bad", 20.0], [], 'invalid_request', None),
            ([60.0, True, 20.0], [], 'invalid_request', None),
            ([60.0, 10 ** 400, 20.0], [], 'invalid_request', None),
        ]:
            with self.subTest(shifts=shifts, groups=groups):
                compound = {"id": 1, "smiles": "CCCO", "dp5_c_signals_list": shifts, "dp5_carbon_groups": groups, "run_dp5": True}
                result, requests = self.enrich(compound)
                self.assertEqual(requests, [])
                self.assertEqual(compound["dp5"]["status"], "skipped")
                self.assertEqual(compound["dp5"]["reason"], expected_reason)
                self.assertNotIn("probability_percent", compound["dp5"])
                self.assertNotIn('score_raw', compound['dp5'])
                self.assertEqual(result["dp5_summary"]["successful"], 0)
                self.assertEqual(result['dp5_summary']['failed'], 0)
                if expected_message is not None:
                    self.assertEqual(compound['dp5']['message'], expected_message)
                    self.assertEqual(compound['dp5']['message_severity'], 'info')
                else:
                    self.assertNotIn('message', compound['dp5'])
                    self.assertIn(expected_reason, ' '.join(compound['all_warnings']))
                    self.assertNotIn('analysis failed', ' '.join(compound['all_warnings']))

    def test_range_endpoint_and_solvent_peak_survive_final_adapter(self):
        for signals, expected in [("130.0–131.0, 20.0", (130.0, 20.0)), ("77.0, 20.0", (77.0, 20.0))]:
            with self.subTest(signals=signals):
                compound = self.parsed_compound(signals, smiles="CCO", count=2)
                _, requests = self.enrich(compound)
                self.assertEqual(requests[0].carbon_shifts, expected)

    def test_invalid_structure_and_absent_carbon_spectra_skip_scoring(self):
        for compound, reason in [
            ({"id": 1, "smiles": "invalid", "dp5_c_signals_list": [60.0]}, "invalid_structure"),
            ({"id": 1, "smiles": "CCO", "dp5_c_signals_list": None}, "carbon_nmr_missing"),
            (self.parsed_compound("60.0", smiles="CCO", count=2), "strict_nmr_coverage_mismatch"),
        ]:
            with self.subTest(reason=reason):
                result, requests = self.enrich(compound)
                self.assertEqual(requests, [])
                self.assertEqual(result["compounds"][0]["dp5"]["reason"], reason)

    def test_malformed_separator_is_a_finding_and_cannot_receive_a_score(self):
        for severity in ["warning", "disabled"]:
            with self.subTest(severity=severity):
                self.cfg.errors.overrides[ErrorCode.NMR_SIGNAL_FORMAT] = ErrorSeverity(severity)
                compound = self.parsed_compound("60.0 20.0", smiles="CCO", count=2)
                self.assertEqual(any("nmr_signal_format." in line for line in compound["all_warnings"]), severity == "warning")
                result, requests = self.enrich(compound)
                self.assertEqual(requests, [])
                self.assertEqual(result["compounds"][0]["dp5"]["status"], "skipped")
                self.assertNotIn("probability_percent", compound["dp5"])

    def test_probability_threshold_and_disabled_finding_preserve_score(self):
        for severity in ["warning", "disabled"]:
            with self.subTest(severity=severity):
                self.cfg.errors.overrides[ErrorCode.DP5_LOW_PROBABILITY] = ErrorSeverity(severity)
                compound = self.parsed_compound("60.0, 20.0", smiles="CCO", count=2)
                result, requests = self.enrich(compound, probability=0.05)
                self.assertEqual(len(requests), 1)
                self.assertEqual(compound["dp5"]["probability_percent"], 5.0)
                self.assertEqual(result["error_stats"]["dp5_low_probability"], int(severity == "warning"))
                self.assertEqual(any("dp5_low_probability." in line for line in compound["all_warnings"]), severity == "warning")

    def test_unconsumed_integer_shift_cannot_receive_a_truncated_score(self):
        for severity in ["warning", "disabled"]:
            with self.subTest(severity=severity):
                self.cfg.errors.overrides[ErrorCode.NMR_SIGNAL_PRECISION_INVALID] = ErrorSeverity(severity)
                compound = self.parsed_compound("60.0, 20.0, 5", smiles="CCO", count=2)
                # Preserve the diagnostic/count grammar while refusing incomplete DP5 input.
                self.assertEqual(compound["dp5_c_signals_list"], [60.0, 20.0])
                self.assertEqual(any("nmr_signal_precision_invalid." in line for line in compound["all_warnings"]), severity == "warning")
                result, requests = self.enrich(compound)
                self.assertEqual(requests, [])
                self.assertEqual(result["compounds"][0]["dp5"]["status"], "skipped")
                self.assertNotIn("probability_percent", compound["dp5"])
