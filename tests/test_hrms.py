"""Characterize HRMS entry points with small, independently calculated fixtures."""

import unittest

from checksi.analysis.hrms import adjust_mass_by_hrms, check_hrms
from checksi.config_schema import AppConfig
from checksi.utils.error_handler import ErrorTracker


def make_config(*, overrides=None, detailed=False):
    """Build isolated settings without loading saved configuration or services."""
    return AppConfig.model_validate({
        "paths": {"input_path": "input.pdf", "output_dir": "."},
        "modes": {"deep_name_analysis": "off", "output_mode": "detailed" if detailed else "essential"},
        "solvents": {"entries": [{"name": "CDCl3", "aliases": [], "proton_shifts": [7.26], "carbon_shifts": [77.0]}]},
        "errors": {"default_severity": "critical", "overrides": overrides or {}},
    })


class HRMSCharacterizationTests(unittest.TestCase):
    """Protect return values, scientific conventions and finding multiplicity."""

    def check_text(self, text, *, cfg=None, output=None):
        tracker = ErrorTracker()
        result = check_hrms(cfg or make_config(), tracker, text, [] if output is None else output)
        return result, tracker

    def test_positive_ion_keeps_formula_atom_mass_in_public_return(self):
        checked, tracker = self.check_text("HRMS (ESI) calcd for C2H7O+ [M+H]+ 47.0491, found 47.0491.")
        self.assertEqual(checked[:2], ([], "C2H7O"))
        # 2*12 + 7*1.00782503223 + 15.99491461957; electron mass
        # is subtracted only for comparison with the reported positive ion.
        self.assertAlmostEqual(checked[2], 47.04968984518, places=8)
        self.assertEqual(sum(tracker.to_dict().values()), 0)

    def test_negative_ion_adds_electron_mass_for_reported_comparison(self):
        checked, tracker = self.check_text("HRMS C2H5O- [M-H]- 45.0346, found 45.0346.")
        self.assertEqual(checked[:2], ([], "C2H5O"))
        self.assertAlmostEqual(checked[2], 45.03403978072, places=8)
        self.assertEqual(sum(tracker.to_dict().values()), 0)

    def test_ion_charge_fallback_and_detailed_explanation_are_preserved(self):
        checked, tracker = self.check_text(
            "HRMS C2H7O [M+H]+ 47.0491, found 47.0491.", cfg=make_config(detailed=True),
        )
        self.assertTrue(any("hrms_formula_charge_missing." in line for line in checked[0]))
        self.assertIn("HRMS mass (from formula): 47.0491 (C2H7O)", checked[0])
        self.assertIn("calcd mass match formula", checked[0])
        self.assertEqual(tracker.get_count("hrms_formula_charge_missing"), 1)
        self.assertEqual(tracker.get_count("calcd_formula_mass_mismatch"), 0)

    def test_charge_mismatch_is_reported_once(self):
        checked, tracker = self.check_text("HRMS C2H7O+ [M+H]- 47.0491, found 47.0491.")
        self.assertEqual(tracker.get_count("hrms_charge_inconsistent"), 1)
        self.assertEqual(len(checked[0]), 1)

    def test_large_ppm_difference_emits_two_existing_findings(self):
        checked, tracker = self.check_text("HRMS C2H7O+ [M+H]+ 47.0491, found 47.0501.")
        self.assertEqual(tracker.get_count("hrms_ppm_too_large"), 2)
        self.assertEqual(len(checked[0]), 2)
        self.assertIn("diff = 21.3 ppm", checked[0][0])

    def test_disabled_ppm_diagnostic_has_no_findings_or_counts(self):
        checked, tracker = self.check_text(
            "HRMS C2H7O+ [M+H]+ 47.0491, found 47.0501.",
            cfg=make_config(overrides={"hrms_ppm_too_large": "disabled"}),
        )
        self.assertEqual(checked[0], [])
        self.assertEqual(tracker.get_count("hrms_ppm_too_large"), 0)

    def test_missing_formula_retains_output_and_zero_mass_return(self):
        output = ["prior finding"]
        checked, tracker = self.check_text("No HRMS section.", output=output)
        self.assertIs(checked[0], output)
        self.assertEqual(checked, (output, None, 0))
        self.assertEqual(sum(tracker.to_dict().values()), 0)

    def test_proton_sodium_and_hydrogen_loss_inverse_compositions(self):
        for text, ion_formula, neutral_formula, mass in [
            ("[M+H]+", "C2H7O", "C2H6O", 46.04186481295),
            ("[M+Na]+", "C2H6NaO", "C2H6O", 46.04186481295),
            ("[M-H]-", "C2H5O", "C2H6O", 46.04186481295),
            ("[M+37Cl]-", "C2H5[37Cl]", "C2H5", 29.03912516115),
        ]:
            with self.subTest(text=text):
                tracker = ErrorTracker()
                checked = adjust_mass_by_hrms(make_config(), tracker, text, [], ion_formula, 0)
                self.assertEqual(checked[:2], ([], neutral_formula))
                self.assertAlmostEqual(checked[2], mass, places=8)
                self.assertEqual(sum(tracker.to_dict().values()), 0)

    def test_inverse_composition_detailed_output_preserves_supplied_list(self):
        output = ["prior finding"]
        checked = adjust_mass_by_hrms(make_config(detailed=True), ErrorTracker(), "[M+H]+", output, "C2H7O", 0)
        self.assertIs(checked[0], output)
        self.assertEqual(output, ["prior finding", "formula HRMS C2H7O, real formula C2H6O"])

    def test_no_adduct_expression_leaves_output_and_returns_no_inverse(self):
        output = ["prior finding"]
        checked = adjust_mass_by_hrms(make_config(), ErrorTracker(), "HRMS C2H6O", output, "C2H6O", 0)
        self.assertEqual(checked, (output, None, None))

    def test_leading_coefficient_preserves_explicit_deuterium(self):
        # Pavel Q10: 2H means two protons in an adduct; [2H] is deuterium.
        checked = adjust_mass_by_hrms(make_config(), ErrorTracker(), "[M+2H]+", [], "C2H5[2H]", 0)
        self.assertEqual(checked[:2], ([], "C2H3[2H]"))
        self.assertAlmostEqual(checked[2], 29.03757687481, places=8)


if __name__ == "__main__":
    unittest.main()
