"""Value-level and diagnostic regressions from the PR 8 source review."""

from dataclasses import asdict
import unittest
from unittest.mock import patch

from rdkit import Chem

from checksi.analysis.nmr import check_nmr, preprocess_signals
from checksi.analysis.stereochem import check_streo_desc
from checksi.analysis.structure_analysis import C13Alternative, analyze_molecular_structure_detailed
from checksi.config_schema import AppConfig
from checksi.dp5_enrichment import enrich_analysis_with_dp5
from checksi.dp5q.contracts import CheckResult
from checksi.utils.error_handler import ErrorTracker


def make_config() -> AppConfig:
    """Use explicit settings without reading a developer's saved configuration."""
    return AppConfig.model_validate({
        "paths": {"input_path": "input.pdf", "output_dir": "."},
        "modes": {"deep_name_analysis": "on", "output_mode": "essential"},
        "solvents": {"entries": [{"name": "CDCl3", "aliases": [], "proton_shifts": [7.26], "carbon_shifts": [77.0]}]},
        "errors": {"default_severity": "critical", "overrides": {}},
        "dp5": {"enabled": True},
    })


def check_carbon(cfg, signals, *, trees_count=None, tracker=None, **kwargs):
    """Exercise the real section, count and validation path."""
    return check_nmr(
        cfg, tracker if tracker is not None else ErrorTracker(),
        f"13C NMR (100 MHz, CDCl3): δ {signals}.", "13C", 1,
        trees_count, 400, 0, 0, kwargs.pop("formula", None), 0, 0, **kwargs,
    )


class CarbonExpansionReviewTests(unittest.TestCase):
    """Assert actual engine-bound shift values rather than just their cardinality."""

    def setUp(self):
        self.cfg = make_config()
        self.runtime = {"model_id": "fixture", "score_version": "dp5q-logtail-v1"}

    def engine_shifts(self, signals, *, count, smiles):
        """Keep parsing and coverage real; capture the typed process-bound request."""
        structure = analyze_molecular_structure_detailed(Chem.AddHs(Chem.MolFromSmiles(smiles)))
        self.assertEqual(structure.carbon_count, count)
        observations = []
        checked = check_carbon(self.cfg, signals, trees_count=count, carbon_groups=structure.carbon_groups,
                               observations_out=observations)
        compound = {
            "id": 1, "smiles": smiles, "all_warnings": checked[0],
            "dp5_carbon_groups": checked[5], "run_dp5": checked[6],
            "dp5_c_signals_list": checked[7],
            "carbon_observations": [asdict(item) for item in observations],
        }
        requests = []

        def check(request):
            requests.append(list(request.carbon_shifts))
            return CheckResult(compound_id=request.compound_id, status="assessed", score_raw=0.42)
        with patch("checksi.dp5_enrichment.check_availability", return_value=self.runtime), patch("checksi.dp5_enrichment.DP5qSession") as session:
            session.return_value.__enter__.return_value.check.side_effect = check
            result = enrich_analysis_with_dp5(self.cfg, {"compounds": [compound], "error_stats": {}})
        self.assertEqual(result["dp5_summary"]["successful"], 1, compound)
        self.assertEqual(len(requests), 1)
        self.assertEqual(compound["dp5"]["probability_percent"], 42.0)
        return checked, requests[0]

    def test_suffix_and_signed_shift_values_survive_the_final_adapter(self):
        for signals, expected in [
            ("14.1 (2C), 114.1 (2C)", [14.1, 14.1, 114.1, 114.1]),
            ("8.5 (2C), -18.5 (2C)", [8.5, 8.5, -18.5, -18.5]),
            ("5.3 (2C), -5.3 (2C)", [5.3, 5.3, -5.3, -5.3]),
            ("128.5 (2C), 28.5 (2C)", [128.5, 128.5, 28.5, 28.5]),
            ("14.1 (2 C), 114.1 (2С)", [14.1, 14.1, 114.1, 114.1]),
        ]:
            with self.subTest(signals=signals):
                # Each symmetry pair is one environment. The legacy helper
                # still expands 2C, while that overlap claim is refused by DP5.
                self.assertEqual(preprocess_signals(signals), expected)
                refused = check_carbon(self.cfg, signals, trees_count=2,
                    carbon_groups=[["C1", "C5"], ["C2", "C4"]])
                self.assertFalse(refused[6])
                self.assertEqual(refused[7], expected[::2])
                accepted = signals.replace("2 C", "1 C").replace("2C", "1C").replace("2С", "1С")
                checked, actual = self.engine_shifts(accepted, count=2, smiles="CCOCC")
                self.assertEqual(checked[7], expected[::2])
                self.assertEqual(actual, expected[::2])

    def test_atom_assignment_labels_are_not_carbon_multiplicities(self):
        for label in ["OCH2CH3", "CH2CH3", "CH3CO", "2CH"]:
            with self.subTest(label=label):
                checked, actual = self.engine_shifts(f"60.4 ({label}), 20.1", count=2, smiles="CCO")
                self.assertEqual(checked[7], [60.4, 20.1])
                self.assertEqual(actual, [60.4, 20.1])
                self.assertTrue(any("C NMR Result: 2." in line for line in checked[0]))


class MultiplicityReviewTests(unittest.TestCase):
    """Separate the leading multiplicity descriptor from later assignment labels."""

    def setUp(self):
        self.cfg = make_config()

    def proton(self, descriptor):
        return check_nmr(
            self.cfg, ErrorTracker(), f"1H NMR (400 MHz, CDCl3): δ 3.50 ({descriptor}).",
            "1H", 0, None, 0, 0, 0, None, 0, 0,
        )[0]

    def test_assignment_labels_do_not_require_coupling_constants(self):
        for descriptor in [
            "s, 3H, p-CH3", "s, 3H, p-OMe", "m, 1H, H-p",
            "br s, 1H, p-OH", "m, 3H, m,p-ArH", "s, 3H, CH3 of p-tolyl",
            "s, 1H, H-h", "s, 9H, t-Bu",
        ]:
            with self.subTest(descriptor=descriptor):
                warnings = self.proton(descriptor)
                self.assertFalse(any("hnmr_j_missing." in line for line in warnings), warnings)
                self.assertFalse(any("hnmr_signal_desc_invalid." in line for line in warnings), warnings)
        for label in ["C-p", "C-h", "t-Bu"]:
            with self.subTest(label=label):
                warnings = check_carbon(self.cfg, f"158.9 ({label})")[0]
                self.assertFalse(any("nmr_j_missing." in line for line in warnings), warnings)

    def test_added_multiplicities_are_valid_in_the_leading_descriptor(self):
        for multiplicity in ["p", "pent", "h", "dhept", "dp", "tp", "ddp", "dddd"]:
            with self.subTest(multiplicity=multiplicity):
                for warnings in [
                    self.proton(f"{multiplicity}, J = 7.2 Hz, 2H"),
                    check_carbon(self.cfg, f"60.0 ({multiplicity}, J = 7.2 Hz)")[0],
                ]:
                    self.assertFalse(any("multiplicity" in line or "signal_desc_invalid." in line or "j_missing." in line for line in warnings), warnings)
                self.assertTrue(any("hnmr_j_missing." in line for line in self.proton(f"{multiplicity}, 2H")))
        self.assertFalse(any("multiplicity" in line for line in self.proton("bs, 1H")))


class NMRPolicyReviewTests(unittest.TestCase):
    """Discriminate the inherited zero-count, alternatives and fraction policies."""

    def setUp(self):
        self.cfg = make_config()

    def test_zero_structure_carbon_count_uses_parsed_name_finding(self):
        checked = check_carbon(self.cfg, "60.0", trees_count=0)
        warnings = checked[0]
        self.assertTrue(any("c13_count_structure_check. Check the parsed name." in line for line in warnings), warnings)
        self.assertFalse(checked[6])
        self.assertFalse(any("probably, it should be 0" in line for line in warnings))

    def test_stereo_alternative_count_list_excludes_the_already_named_count(self):
        checked = check_carbon(
            self.cfg, "60.0", trees_count=3, c13_count_alternatives={2, 3},
            stereogenic_signal_flag=True,
        )
        finding = next(line for line in checked[0] if "c13_count_structure_check." in line)
        self.assertIn("3 or 2", finding)
        self.assertNotIn("3 or 2 or 3", finding)

    def test_lower_partition_retains_plain_manual_stereo_finding_without_replacing_groups(self):
        alternatives = [
            C13Alternative("first", 2, [["C1", "C2"]]),
            C13Alternative("second", 2, [["C2", "C3"]]),
        ]
        checked = check_carbon(
            self.cfg, "60.0, 20.0", trees_count=3,
            c13_count_alternatives_w_groups=alternatives,
            stereogenic_signal_flag=True,
        )
        self.assertFalse(checked[6])
        self.assertEqual(checked[5], [])
        self.assertEqual(checked[7], [60., 20.])
        finding = next(line for line in checked[0] if "c13_count_structure_check_stereo." in line)
        self.assertIn("manual verification", finding)
        self.assertNotIn("[['", finding)
        self.assertNotIn("DP5", finding)

    def test_out_of_domain_equivalent_fraction_is_omitted_and_warnings_remain(self):
        for ratio in [-0.1, 9 / 8, 100, float("inf"), float("nan")]:
            with self.subTest(ratio=ratio):
                tracker = ErrorTracker()
                findings = check_streo_desc(self.cfg, tracker, "(S)-1-phenylethan-1-ol", ratio, 0, 1)
                self.assertEqual(tracker.get_count("stereo_check_required"), 1)
                self.assertTrue(any("Verify whether" in line for line in findings), findings)
                self.assertFalse(any("Fraction of equivalent carbons" in line for line in findings), findings)
        tracker = ErrorTracker()
        checked = check_carbon(self.cfg, "150.0, 140.0, 130.0, 120.0, 110.0, 100.0, 90.0, 80.0, 70.0", formula="C8H10O", tracker=tracker)
        self.assertEqual(checked[4], 9 / 8)
        self.assertEqual(tracker.get_count("c13_count_exceeds_formula"), 1)
        self.assertTrue(any("c13_count_exceeds_formula." in line for line in checked[0]))

    def test_equivalent_fraction_cannot_supply_a_structural_meso_assessment(self):
        findings = check_streo_desc(self.cfg, ErrorTracker(), "(S)-1-phenylethan-1-ol", 0.5, 0, 1)
        self.assertTrue(any("unassessed" in line for line in findings), findings)
        self.assertFalse(any("Fraction of equivalent carbons" in line for line in findings), findings)

    def test_dp5_refusal_cause_priority_is_separators_then_tokens_then_count(self):
        for signals, expected in [
            ("60.0 20.0, 5", "malformed_separators"),
            ("60.0, 5", "incomplete_numeric_tokens"),
            ("60.0", "count_mismatch"),
        ]:
            with self.subTest(signals=signals):
                details = {}
                checked = check_carbon(self.cfg, signals, trees_count=3, dp5_refusal_details=details)
                self.assertFalse(checked[6])
                self.assertEqual(details["cause"], expected)

    def test_dp5_refusal_details_preserve_tuple_shape_and_specific_causes(self):
        for signals, count, expected in [
            ("60.0", 2, {"cause": "count_mismatch", "observed_count": 1, "prepared_shift_count": 1, "required_count": 2}),
            ("60.0 20.0", 2, {"cause": "malformed_separators", "observed_count": 2, "prepared_shift_count": 2, "required_count": 2}),
            ("60.0, 20.0, 5", 2, {"cause": "incomplete_numeric_tokens", "observed_count": 2, "prepared_shift_count": 2, "required_count": 2}),
        ]:
            with self.subTest(signals=signals):
                details = {"stale": True}
                checked = check_carbon(self.cfg, signals, trees_count=count, dp5_refusal_details=details)
                self.assertEqual(len(checked), 8)
                self.assertFalse(checked[6])
                self.assertEqual(details, expected)
        details = {"stale": True}
        checked = check_carbon(self.cfg, "60.0, 20.0", trees_count=2, carbon_groups=[], dp5_refusal_details=details)
        self.assertTrue(checked[6])
        self.assertEqual(details, {})
        checked = check_carbon(
            self.cfg, "60.0 20.0", trees_count=3,
            c13_count_alternatives_w_groups=[C13Alternative("selected", 2, [["C1", "C2"]])],
            stereogenic_signal_flag=True, dp5_refusal_details=details,
        )
        self.assertFalse(checked[6])
        self.assertEqual(details, {
            "cause": "malformed_separators", "observed_count": 2,
            "prepared_shift_count": 2, "required_count": 3,
        })
