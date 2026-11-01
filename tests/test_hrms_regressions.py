"""Regression cases for HRMS numeric validity and inverse compositions."""

import math
import sys
import unittest

from checksi.analysis.hrms import adjust_mass_by_hrms, check_hrms
from checksi.analysis.hrms_measurements import check_reported_masses
from checksi.utils.error_handler import ErrorTracker
from tests.test_hrms import make_config


class HRMSMeasurementRegressionTests(unittest.TestCase):
    """Use reported values with exact decimal boundaries, separate from formula mass."""

    def measure(self, text, formula_mass=None):
        tracker = ErrorTracker()
        output = check_reported_masses(make_config(), tracker, text, [], formula_mass, None, None, None)
        return output, tracker

    def test_exact_ten_ppm_does_not_report_large_difference(self):
        # 0.0010 / 100.0000 * 10**6 is exactly 10 ppm, for either sign.
        for found in ["100.0010", "99.9990"]:
            with self.subTest(found=found):
                output, tracker = self.measure(f"100.0000, found {found}.")
                self.assertEqual(output, [])
                self.assertEqual(tracker.get_count("hrms_ppm_too_large"), 0)

    def test_ppm_values_just_inside_and_outside_boundary(self):
        for found, count in [("100.0009", 0), ("99.9991", 0), ("100.0011", 2), ("99.9989", 2)]:
            with self.subTest(found=found):
                _, tracker = self.measure(f"100.0000, found {found}.")
                self.assertEqual(tracker.get_count("hrms_ppm_too_large"), count)

    def test_calculated_mass_at_typo_boundary_stays_in_typo_branch(self):
        # Both differences are exactly 0.0002 Da after the existing 4-place rounding.
        for calculated in ["99.9998", "100.0002"]:
            with self.subTest(calculated=calculated):
                output, tracker = self.measure(f"{calculated}, found {calculated}.", formula_mass=100.0)
                self.assertEqual(tracker.get_count("calcd_formula_mass_mismatch"), 1)
                self.assertEqual(tracker.get_count("calcd_formula_mass_delta"), 0)
                self.assertIn("Possible typo.", output[0])

    def test_calculated_mass_beyond_typo_boundary_reports_difference(self):
        output, tracker = self.measure("47.0494, found 47.0494.", formula_mass=47.0491)
        self.assertEqual(tracker.get_count("calcd_formula_mass_mismatch"), 1)
        self.assertEqual(tracker.get_count("calcd_formula_mass_delta"), 1)
        self.assertTrue(any("difference" in line for line in output))

    def test_valid_calculated_mass_is_retained_when_found_is_missing(self):
        for text in ["47.0491", "47.0491.", "47.0491, found", "47.0491, found nope."]:
            with self.subTest(text=text):
                _, tracker = self.measure(text)
                self.assertEqual(tracker.get_count("hrms_calcd_mass_missing"), 0)
                self.assertEqual(tracker.get_count("hrms_found_mass_missing"), 1)

    def test_zero_negative_and_malformed_calculated_values_are_invalid(self):
        for token in ["0.0000", "-47.0491", "–47.0491", "47.04912", "x47.0491", "47.0491.2"]:
            with self.subTest(token=token):
                _, tracker = self.measure(f"{token}, found 47.0491.")
                self.assertEqual(tracker.get_count("hrms_calcd_mass_missing"), 1)
                self.assertEqual(tracker.get_count("hrms_found_mass_missing"), 0)
                self.assertEqual(tracker.get_count("hrms_ppm_too_large"), 0)

    def test_found_mass_does_not_borrow_unrelated_later_numbers(self):
        for token in ["nope; NMR 4.1234", "0.0000", "-47.0491", "47.04912", "47.0491.2"]:
            with self.subTest(token=token):
                _, tracker = self.measure(f"47.0491, found {token}.")
                self.assertEqual(tracker.get_count("hrms_calcd_mass_missing"), 0)
                self.assertEqual(tracker.get_count("hrms_found_mass_missing"), 1)
                self.assertEqual(tracker.get_count("hrms_ppm_too_large"), 0)

    def test_each_missing_field_is_reported_independently(self):
        for text in ["calcd nope; found nope.", "no mass fields"]:
            with self.subTest(text=text):
                _, tracker = self.measure(text)
                self.assertEqual(tracker.get_count("hrms_calcd_mass_missing"), 1)
                self.assertEqual(tracker.get_count("hrms_found_mass_missing"), 1)


class HRMSIonRegressionTests(unittest.TestCase):
    """Reject impossible compositions and retain explicit isotopic identity."""

    def test_negative_inverse_count_never_returns_a_fabricated_formula(self):
        for severity in ["disabled", "info", "warning", "critical"]:
            with self.subTest(severity=severity):
                cfg = make_config(overrides={"hrms_ion_balance_invalid": severity})
                tracker = ErrorTracker()
                output, formula, mass = adjust_mass_by_hrms(cfg, tracker, "[M+H2]+", [], "C2H", 0)
                self.assertIsNone(formula)
                self.assertIsNone(mass)
                self.assertEqual(tracker.get_count("hrms_ion_balance_invalid"), int(severity != "disabled"))
                self.assertEqual(len(output), int(severity != "disabled"))

    def test_isotope_brackets_survive_inverse_composition_serialization(self):
        output, formula, mass = adjust_mass_by_hrms(make_config(), ErrorTracker(), "[M+H]+", [], "C2H6[34S]", 0)
        self.assertEqual(output, [])
        self.assertEqual(formula, "C2H5[34S]")
        # 2*12 + 5*1.00782503223 + sulfur-34 mass 33.967867004.
        self.assertAlmostEqual(mass, 63.00699216515, places=7)

    def test_bracketed_isotopic_adduct_includes_the_entire_fragment(self):
        output, formula, mass = adjust_mass_by_hrms(make_config(), ErrorTracker(), "[M+[13C]H3]+", [], "C2H7[13C]O", 0)
        self.assertEqual(output, [])
        self.assertEqual(formula, "C2H4O")
        # Remove one carbon-13 and three ordinary H atoms from the ion formula.
        self.assertAlmostEqual(mass, 44.02621474849, places=8)

    def test_empty_inverse_composition_is_invalid_without_raising(self):
        output, formula, mass = adjust_mass_by_hrms(make_config(), ErrorTracker(), "[M+H]+", [], "H", 0)
        self.assertIsNone(formula)
        self.assertIsNone(mass)

    def test_krypton_does_not_trigger_a_potassium_adduct_mismatch(self):
        tracker = ErrorTracker()
        _, formula, _ = adjust_mass_by_hrms(make_config(), tracker, "[M+H]+", [], "C2H7Kr", 0)
        self.assertEqual(formula, "C2H6Kr")
        self.assertEqual(tracker.get_count("hrms_adduct_mismatch"), 0)


class HRMSFormulaValidityRegressionTests(unittest.TestCase):
    """Diagnostic visibility never determines validity or safe numeric flow."""

    def test_invalid_formula_stops_mass_arithmetic_for_every_diagnostic_severity(self):
        for severity in ["disabled", "info", "warning", "critical"]:
            with self.subTest(severity=severity):
                cfg = make_config(overrides={"formula_atom_symbol_invalid": severity})
                tracker = ErrorTracker()
                for _ in range(2):
                    _, formula, mass = check_hrms(cfg, tracker, "HRMS C2H6Xx+ [M+H]+ 47.0491, found 47.0491.", [])
                    self.assertIsNone(formula)
                    self.assertIsNone(mass)
                self.assertEqual(tracker.get_count("formula_atom_symbol_invalid"), 2 * int(severity != "disabled"))
                self.assertEqual(tracker.get_count("calcd_formula_mass_mismatch"), 0)

    def test_explicit_isotope_label_is_not_removed_with_the_adduct_brackets(self):
        tracker = ErrorTracker()
        output, formula, mass = check_hrms(
            make_config(), tracker, "HRMS C2H5[37Cl]+ [M+H]+ 66.0045, found 66.0045.", [],
        )
        self.assertEqual(formula, "C2H5[37Cl]")
        self.assertAlmostEqual(mass, 66.00502776315, places=8)
        self.assertEqual(output, [])

    def test_formula_isotope_bracket_does_not_count_as_an_ion_expression(self):
        tracker = ErrorTracker()
        output, _, _ = check_hrms(
            make_config(), tracker, "HRMS C2H5[37Cl]+ 66.0045, found 66.0045.", [],
        )
        self.assertEqual(tracker.get_count("hrms_ion_charge_missing"), 0)
        self.assertFalse(any("hrms_ion_charge_missing." in line for line in output))


class HRMSMeasurementAssociationRegressionTests(unittest.TestCase):
    """Associate each mass field with its existing HRMS section marker."""

    def test_prior_elemental_found_field_does_not_capture_hrms_measurements(self):
        text = (
            "Anal. Calcd for C2H6O: C, 52.14; H, 13.13. Found: C, 52.14; H, 13.13. "
            "HRMS (ESI) calcd for C2H7O+ [M+H]+ 47.0491, found 47.0501."
        )
        tracker = ErrorTracker()
        output, formula, mass = check_hrms(make_config(), tracker, text, [])
        self.assertEqual(formula, "C2H7O")
        self.assertAlmostEqual(mass, 47.04968984518, places=8)
        self.assertEqual(tracker.get_count("hrms_calcd_mass_missing"), 0)
        self.assertEqual(tracker.get_count("hrms_found_mass_missing"), 0)
        self.assertEqual(tracker.get_count("hrms_ppm_too_large"), 2)
        self.assertEqual(len(output), 2)

    def test_later_elemental_or_nmr_found_field_does_not_repair_missing_hrms_found(self):
        for unrelated in [
            "Anal. Calcd for C2H6O: C, 52.14. Found: C, 52.14.",
            "1H NMR (400 MHz, CDCl3): 7.1234; found 47.0491.",
            "13C-NMR (100 MHz, CDCl3): 47.0491; found 47.0491.",
        ]:
            with self.subTest(unrelated=unrelated):
                tracker = ErrorTracker()
                check_hrms(make_config(), tracker, f"HRMS C2H7O+ [M+H]+ 47.0491; {unrelated}", [])
                self.assertEqual(tracker.get_count("hrms_calcd_mass_missing"), 0)
                self.assertEqual(tracker.get_count("hrms_found_mass_missing"), 1)
                self.assertEqual(tracker.get_count("hrms_ppm_too_large"), 0)

    def test_later_nmr_numbers_do_not_supply_both_missing_hrms_fields(self):
        tracker = ErrorTracker()
        check_hrms(
            make_config(), tracker,
            "HRMS C2H7O+ [M+H]+ missing; 1H NMR (400 MHz, CDCl3): 47.0491, found 47.0491.", [],
        )
        self.assertEqual(tracker.get_count("hrms_calcd_mass_missing"), 1)
        self.assertEqual(tracker.get_count("hrms_found_mass_missing"), 1)

    def test_words_containing_esi_do_not_select_a_header_before_nmr(self):
        for word in ["resin", "residue", "Mesityl"]:
            with self.subTest(word=word):
                tracker = ErrorTracker()
                output, _, _ = check_hrms(
                    make_config(), tracker,
                    f"Yellow {word}. 1H NMR (400 MHz, CDCl3): δ 3.50 (s, 2H). "
                    "HRMS (ESI): calcd for C2H7O+ [M+H]+ 47.0491, found 47.0501.", [],
                )
                self.assertEqual(tracker.get_count("hrms_calcd_mass_missing"), 0)
                self.assertEqual(tracker.get_count("hrms_found_mass_missing"), 0)
                self.assertEqual(tracker.get_count("hrms_ppm_too_large"), 2)
                self.assertEqual(len(output), 2)


class HRMSNumericCapacityRegressionTests(unittest.TestCase):
    """Unsupported numeric capacity returns no inverse, independent of findings."""

    def test_unrepresentable_fragment_or_reconstructed_mass_returns_no_inverse(self):
        conversion_digits = max(sys.get_int_max_str_digits() + 1, 4301)
        texts = {
            "fragment_float_conversion": "[M-H" + "9" * 350 + "]+",
            "fragment_integer_conversion": "[M-H" + "9" * conversion_digits + "]+",
            "reconstructed_count_conversion": "[M-H" + "9" * 308 + "-H" + "9" * 308 + "]+",
            "nonfinite_reconstructed_mass": "[M-O" + "9" * 308 + "]+",
        }
        for severity in ["disabled", "info", "warning", "critical"]:
            for case, text in texts.items():
                with self.subTest(severity=severity, case=case):
                    cfg = make_config(overrides={"hrms_ion_balance_invalid": severity})
                    tracker = ErrorTracker()
                    output = ["prior finding"]
                    checked = adjust_mass_by_hrms(cfg, tracker, text, output, "C2H7O", 0)
                    self.assertEqual(checked, (output, None, None))
                    self.assertEqual(output, ["prior finding"])
                    self.assertEqual(sum(tracker.to_dict().values()), 0)

    def test_large_finite_mass_is_not_rejected_by_an_arbitrary_count_limit(self):
        checked = adjust_mass_by_hrms(make_config(), ErrorTracker(), "[M-H" + "9" * 300 + "]+", [], "C2H7O", 0)
        self.assertEqual(checked[1], f"C2H{10**300 + 6}O")
        self.assertTrue(math.isfinite(checked[2]))
        self.assertAlmostEqual(checked[2] / 1e300, 1.00782503223, places=10)


if __name__ == "__main__":
    unittest.main()
