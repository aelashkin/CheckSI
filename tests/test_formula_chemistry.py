"""Direct formula/isotope checks using NIST reference masses, not generated oracles.

Reference tables: https://physics.nist.gov/PhysRefData/Compositions/index.html
The tolerances accommodate rounding and the locked library's mass table.
"""

import unittest

from checksi.analysis.formula_utils import (
    count_carbon,
    count_hydrogen,
    get_isotopic_masses,
    get_iupac_formula_order_warnings,
    replace_common_isotope_formats,
    validate_formula_and_get_mass,
)
from checksi.analysis.mass_spec_utils import ELEMENT_MASSES, find_elemental_combinations_for_mass
from checksi.config_schema import AppConfig
from checksi.utils.error_handler import ErrorTracker


def chemistry_config(severity="warning"):
    """Build a headless configuration without reading settings or resolving names."""
    return AppConfig.model_validate({
        "paths": {"input_path": "input.pdf", "output_dir": "."},
        "modes": {"deep_name_analysis": "off", "output_mode": "essential"},
        "solvents": {"entries": []},
        "errors": {"default_severity": severity},
    })


class FormulaCharacterizationTests(unittest.TestCase):
    """Protect public tuple shapes, normalization and established ordering policy."""

    def test_ethanol_monoisotopic_mass(self):
        # 2*12 + 6*1.00782503223 + 15.99491461957 (12C, 1H, 16O).
        mass, warning, formatting = validate_formula_and_get_mass(
            chemistry_config(), ErrorTracker(), "C2H6O",
        )
        self.assertAlmostEqual(mass, 46.041864813, places=7)
        self.assertIsNone(warning)
        self.assertEqual(formatting, [])

    def test_explicit_heavy_isotope_mass(self):
        mass, warning, _ = validate_formula_and_get_mass(
            chemistry_config(), ErrorTracker(), "C2H4[18O]",
        )
        # 2*12 + 4*1.00782503223 + 17.99915961286.
        self.assertAlmostEqual(mass, 46.030459742, places=7)
        self.assertIsNone(warning)

    def test_invalid_formula_keeps_none_mass_at_all_diagnostic_levels(self):
        for severity in ("disabled", "info", "warning", "critical"):
            with self.subTest(severity=severity):
                tracker = ErrorTracker()
                mass, warning, formatting = validate_formula_and_get_mass(
                    chemistry_config(severity), tracker, "C2H6Xx",
                )
                self.assertIsNone(mass)
                self.assertEqual(bool(warning), severity != "disabled")
                self.assertEqual(tracker.get_count("formula_atom_symbol_invalid"), int(severity != "disabled"))
                self.assertEqual(formatting, [])

    def test_formula_order_and_duplicate_findings(self):
        for formula, code in (("H6C2O", "formula_order_invalid"), ("C2H4CH2", "formula_duplicate_elements")):
            with self.subTest(formula=formula):
                tracker = ErrorTracker()
                line = get_iupac_formula_order_warnings(chemistry_config(), tracker, formula)
                self.assertIn(code + ".", line)
                self.assertEqual(tracker.get_count(code), 1)

    def test_isotope_normalization_is_idempotent(self):
        normalized = replace_common_isotope_formats("C2H535Cl")
        self.assertEqual(normalized, "C2H5[35Cl]")
        self.assertEqual(replace_common_isotope_formats(normalized), normalized)

    def test_chlorine_variants_include_the_mixed_pair(self):
        variants = dict(get_isotopic_masses("C2H4Cl2"))
        self.assertEqual(len(variants), 3)
        self.assertAlmostEqual(variants["C2H4[35Cl][37Cl]"], 99.966055413, places=6)

    def test_atom_count_public_helpers(self):
        self.assertEqual(count_carbon("C2H6O"), 2)
        self.assertEqual(count_hydrogen("C2H6O"), 6)


class FormulaRegressionTests(unittest.TestCase):
    """Invalidity and isotope composition do not depend on emitted messages."""

    def test_invalid_order_input_stops_at_every_diagnostic_level(self):
        for severity in ("disabled", "info", "warning", "critical"):
            for formula in ("C2Xx6", "C2("):
                with self.subTest(severity=severity, formula=formula):
                    tracker = ErrorTracker()
                    warning = get_iupac_formula_order_warnings(chemistry_config(severity), tracker, formula)
                    self.assertEqual(bool(warning), severity != "disabled")
                    self.assertEqual(tracker.get_count("formula_invalid_format"), int(severity != "disabled"))
                    self.assertEqual(tracker.get_count("formula_order_invalid"), 0)

    def test_unknown_isotope_and_zero_counts_are_invalid(self):
        for formula in ("C2H6[999O]", "C0", "C2H0"):
            with self.subTest(formula=formula):
                mass, _, _ = validate_formula_and_get_mass(chemistry_config("disabled"), ErrorTracker(), formula)
                self.assertIsNone(mass)

    def test_repeated_element_tokens_include_mixed_isotope(self):
        variants = dict(get_isotopic_masses("C2H4ClCl"))
        self.assertEqual(len(variants), 3)
        self.assertAlmostEqual(variants["C2H4[35Cl][37Cl]"], 99.966055413, places=6)

    def test_explicit_isotopes_are_preserved_when_generating_variants(self):
        variants = dict(get_isotopic_masses("C2H4[37Cl]Cl"))
        self.assertEqual(len(variants), 2)
        self.assertTrue(all("[37Cl]" in formula for formula in variants))
        self.assertAlmostEqual(variants["C2H4[37Cl][35Cl]"], 99.966055413, places=6)
        self.assertEqual(get_isotopic_masses("C2H4[18O]"), [])

    def test_sodium_does_not_become_nitrogen(self):
        variants = dict(get_isotopic_masses("C2H5NaO"))
        self.assertEqual(set(variants), {"C2H5Na[16O]", "C2H5Na[18O]"})
        self.assertAlmostEqual(variants["C2H5Na[18O]"], 70.028054056, places=6)

    def test_bracketed_deuterium_normalization_is_idempotent(self):
        self.assertEqual(replace_common_isotope_formats("C6H5[2H]"), "C6H5[2H]")

    def test_group_multipliers_preserve_isotope_atom_counts(self):
        for formula in ("C2H4(Cl)2", "C2H4[Cl]2"):
            with self.subTest(formula=formula):
                variants = get_isotopic_masses(formula)
                self.assertEqual(len(variants), 3)
                self.assertEqual(sorted(round(mass, 6) for _, mass in variants), [97.969005, 99.966055, 101.963105])

    def test_discrepancy_search_uses_most_abundant_boron_isotope(self):
        self.assertAlmostEqual(ELEMENT_MASSES["B"], 11.00930536, places=6)
        self.assertIn([("B", 1)], find_elemental_combinations_for_mass(11.00930536, max_count=1))

    def test_formula_counts_outside_numeric_capacity_return_no_mass(self):
        for severity in ("disabled", "warning"):
            with self.subTest(severity=severity):
                mass, warning, _ = validate_formula_and_get_mass(
                    chemistry_config(severity), ErrorTracker(), "C" + "9" * 350,
                )
                self.assertIsNone(mass)
                self.assertEqual(bool(warning), severity != "disabled")
        self.assertEqual(get_isotopic_masses("C" + "9" * 350 + "Cl"), [])

    def test_unexplainable_large_or_nonfinite_mass_deltas_have_no_combinations(self):
        for difference in (1e308, float("inf"), float("nan")):
            with self.subTest(difference=difference):
                self.assertEqual(find_elemental_combinations_for_mass(difference), [])


if __name__ == "__main__":
    unittest.main()
