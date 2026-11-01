"""Regression coverage for accepted charge and ion forms in the PR 13 review."""

import unittest

from checksi.analysis.hrms import adjust_mass_by_hrms, check_hrms
from checksi.analysis.hrms_ions import extract_ion_fragments
from checksi.utils.error_handler import ErrorTracker
from tests.test_hrms import make_config


class AcceptedIonChargeTests(unittest.TestCase):
    """A bare isotope label is distinct from a charged ion or molecular formula."""

    def test_dimer_and_bracketed_formula_charges_correct_unsigned_formula_mass(self):
        # The dimer fixtures use two ethanol molecules. Atom masses use 12C=12,
        # 1H=1.00782503223, 16O=15.99491461957, 23Na=22.989769282.
        # Subtract 0.00054858 Da for a singly positive charge, then round to 4 dp.
        for ion, ion_formula, atom_mass, reported in [
            ("[2M+Na]+", "C4H12NaO2", 115.07349890790, "115.0730"),
            ("[2M+H]+", "C4H13O2", 93.09155465813, "93.0910"),
            ("[C2H7O]+", "C2H7O", 47.04968984518, "47.0491"),
        ]:
            with self.subTest(ion=ion):
                tracker = ErrorTracker()
                output, formula, mass = check_hrms(
                    make_config(), tracker,
                    f"HRMS (ESI) {ion} calcd for {ion_formula} {reported}, found {reported}.", [],
                )
                self.assertEqual(formula, ion_formula)
                self.assertAlmostEqual(mass, atom_mass, places=8)
                # The bracketed elemental formula itself owns its written sign.
                expected_missing = int(ion != '[C2H7O]+')
                self.assertEqual(tracker.get_count("hrms_formula_charge_missing"), expected_missing)
                self.assertEqual(tracker.get_count("hrms_ion_charge_missing"), 0)
                self.assertEqual(tracker.get_count("calcd_formula_mass_mismatch"), 0)
                self.assertEqual(len(output), expected_missing)

    def test_isotope_bracket_cannot_supply_missing_ion_charge(self):
        tracker = ErrorTracker()
        check_hrms(make_config(), tracker, "HRMS C2H5[37Cl]+ 66.0045, found 66.0045.", [])
        self.assertEqual(tracker.get_count("hrms_ion_charge_missing"), 0)


class AcceptedInverseIonTests(unittest.TestCase):
    """Recover independently computed neutral compositions through accepted wrappers."""

    def test_wrapped_and_internal_charge_adducts_recover_neutral_composition(self):
        for ion, formula, neutral in [
            ("([M+H]+)", "C2H7O", "C2H6O"),
            ("(ESI, [M+H]+)", "C2H7O", "C2H6O"),
            ("([M-H]-)", "C2H5O", "C2H6O"),
            ("([M+Na]+)", "C2H6NaO", "C2H6O"),
            ("[M+H+]", "C2H7O", "C2H6O"),
            ("(M+H+)", "C2H7O", "C2H6O"),
            ("[M+Na+]", "C2H6NaO", "C2H6O"),
        ]:
            with self.subTest(ion=ion):
                output, actual, mass = adjust_mass_by_hrms(make_config(), ErrorTracker(), ion, [], formula, 0)
                self.assertEqual(output, [])
                self.assertEqual(actual, neutral)
                # 2*12 + 6*1.00782503223 + 15.99491461957.
                self.assertAlmostEqual(mass, 46.04186481295, places=8)

    def test_ei_charge_only_ions_leave_atom_composition_unchanged(self):
        for ion in ["(M+)", "[M+]", "(M•+)"]:
            with self.subTest(ion=ion):
                self.assertEqual(extract_ion_fragments(ion), [])
                output, formula, mass = adjust_mass_by_hrms(make_config(), ErrorTracker(), ion, [], "C2H6O", 0)
                self.assertEqual(output, [])
                self.assertEqual(formula, "C2H6O")
                self.assertAlmostEqual(mass, 46.04186481295, places=8)

    def test_nested_isotopic_fragment_keeps_all_atoms_and_following_fragment(self):
        self.assertEqual(
            extract_ion_fragments("[M+[13C]H3+C2H3N]+"),
            [("+", "[13C]H3"), ("+", "C2H3N")],
        )

    def test_missing_added_element_returns_no_inverse_with_one_balance_finding(self):
        for severity in ["disabled", "info", "warning", "critical"]:
            with self.subTest(severity=severity):
                tracker = ErrorTracker()
                cfg = make_config(overrides={"hrms_ion_balance_invalid": severity})
                output, formula, mass = adjust_mass_by_hrms(cfg, tracker, "[M+Na]+", [], "C2H7O", 0)
                self.assertIsNone(formula)
                self.assertIsNone(mass)
                self.assertEqual(tracker.get_count("hrms_ion_balance_invalid"), int(severity != "disabled"))
                matching = [line for line in output if "hrms_ion_balance_invalid." in line]
                self.assertEqual(len(matching), int(severity != "disabled"))
                if matching:
                    self.assertIn("Element 'Na' not found", matching[0])

    def test_first_invalid_element_policy_stops_before_later_invalid_elements(self):
        for severity in ["disabled", "info", "warning", "critical"]:
            with self.subTest(severity=severity):
                tracker = ErrorTracker()
                cfg = make_config(overrides={"hrms_ion_balance_invalid": severity})
                output, formula, mass = adjust_mass_by_hrms(cfg, tracker, "[M+H2O]+", [], "C2H", 0)
                self.assertIsNone(formula)
                self.assertIsNone(mass)
                self.assertEqual(tracker.get_count("hrms_ion_balance_invalid"), int(severity != "disabled"))
                if output:
                    self.assertIn("Quantity of element 'H'", output[0])
                    self.assertNotIn("element 'O'", output[0])

    def test_na_k_identity_findings_survive_an_unparsable_fragment(self):
        for text, formula, expected, balance_count in [
            ("[M+Na+DMSO]+", "C2H7O", "Found M + Na", 1),
            ("[M+K+DMSO]+", "C2H7O", "Found M + K", 1),
            ("[M+DMSO]+", "C2H6NaO", "molecular formula contains Na", 0),
            ("[M+DMSO]+", "C2H6KO", "molecular formula contains K", 0),
        ]:
            with self.subTest(text=text, formula=formula):
                tracker = ErrorTracker()
                output, actual, mass = adjust_mass_by_hrms(make_config(), tracker, text, [], formula, 0)
                self.assertIsNone(actual)
                self.assertIsNone(mass)
                self.assertEqual(tracker.get_count("hrms_adduct_mismatch"), 1)
                self.assertEqual(tracker.get_count("hrms_ion_balance_invalid"), balance_count)
                self.assertTrue(any(expected in line for line in output), output)

    def test_na_coefficient_does_not_create_a_false_absent_adduct_finding(self):
        tracker = ErrorTracker()
        adjust_mass_by_hrms(make_config(), tracker, "[M+2Na-H]+", [], "C2H6NaO", 0)
        self.assertEqual(tracker.get_count("hrms_adduct_mismatch"), 0)


class UnchargedMassComparisonTests(unittest.TestCase):
    """Preserve rounding for equality and unrounded mass for typo classification."""

    def test_uncharged_exact_four_decimal_formula_mass_is_equal(self):
        tracker = ErrorTracker()
        check_hrms(make_config(), tracker, "HRMS (EI) calcd for C2H6O 46.0419, found 46.0419.", [])
        self.assertEqual(tracker.get_count("calcd_formula_mass_mismatch"), 0)

    def test_uncharged_typo_threshold_uses_unrounded_formula_mass(self):
        tracker = ErrorTracker()
        output, _, _ = check_hrms(make_config(), tracker, "HRMS (EI) calcd for C2H6O 46.0421, found 46.0421.", [])
        # 46.0421 - 46.04186481295 = 0.00023518705 > 0.0002.
        self.assertEqual(tracker.get_count("calcd_formula_mass_mismatch"), 1)
        self.assertEqual(tracker.get_count("calcd_formula_mass_delta"), 1)
        self.assertFalse(any("Possible typo" in line for line in output))


if __name__ == "__main__":
    unittest.main()
