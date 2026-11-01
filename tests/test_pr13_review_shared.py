"""Shared formula regressions identified in the PR #13 review."""

import unittest

from checksi.analysis.formula_utils import (
    count_carbon, get_isotopic_masses, get_iupac_formula_order_warnings,
    validate_formula_and_get_mass,
)
from checksi.analysis.hrms_fields import find_hrms_formula
from checksi.analysis.mass_spec_utils import find_elemental_combinations_for_mass
from checksi.utils.error_handler import ErrorTracker
from tests.test_formula_chemistry import chemistry_config


class ReviewSharedTests(unittest.TestCase):
    """Exercise the application helpers rather than generated report snapshots."""

    def test_unclosed_bracket_does_not_hide_identified_formula(self):
        text = "HRMS (ESI) m/z: [M + H+ Calcd for C21H29N2 309.2480; Found 309.2418."
        self.assertEqual(find_hrms_formula(text).formula, "C21H29N2")

    def test_identification_retains_complete_spaced_isotope_refusals(self):
        for formula in ("[13C]6 H6", "C [13C]H5O"):
            with self.subTest(formula=formula):
                self.assertIsNone(find_hrms_formula("HRMS (ESI) calcd for " + formula + " 47.0491, found 47.0491."))

    def test_carbon_count_includes_explicit_isotopes(self):
        self.assertEqual(count_carbon("C5[13C]H13O2"), 6)
        self.assertEqual(count_carbon("[13C]6H6"), 6)

    def test_dot_adduct_multiplier_preserves_two_chlorines(self):
        masses = sorted(round(mass, 6) for _, mass in get_isotopic_masses("C2H4.2HCl"))
        # C2H6Cl2: 24 + 6*1.00782503223 + 2*34.968852682;
        # replace zero, one, or two 35Cl atoms with 37Cl (36.965902602).
        self.assertEqual(masses, [99.984656, 101.981705, 103.978755])

    def test_disabling_duplicate_does_not_hide_other_checks(self):
        for formula, code in (("C2H5OCH2Br", "formula_order_invalid"), ("C2H4CH2Q", "formula_invalid_format")):
            for severity in ("disabled", "info", "warning", "critical"):
                with self.subTest(formula=formula, severity=severity):
                    cfg = chemistry_config()
                    cfg.errors.overrides = {"formula_duplicate_elements": severity}
                    tracker = ErrorTracker()
                    line = get_iupac_formula_order_warnings(cfg, tracker, formula)
                    self.assertIn(code + ".", line)
                    self.assertEqual(tracker.get_count(code), 1)

    def test_finite_guard_handles_an_infinite_mass_without_conversion_error(self):
        tracker = ErrorTracker()
        mass, message, _ = validate_formula_and_get_mass(chemistry_config(), tracker, "C" + "9" * 308)
        self.assertIsNone(mass)
        self.assertIn("formula_atom_symbol_invalid.", message)

    def test_maximal_three_element_discrepancy_is_not_cut_off(self):
        # Three largest supported atomic masses: I, 81Br, 79Br, each count 7.
        target = 7 * (126.9044719 + 80.91629 + 78.91834)
        expected = sorted([("I", 7), ("Br-81", 7), ("Br-79", 7)])
        self.assertIn(expected, find_elemental_combinations_for_mass(target, tolerance=0.0002))
