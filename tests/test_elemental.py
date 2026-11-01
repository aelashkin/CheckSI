"""Elemental analysis chemistry and stable diagnostic characterization."""

import unittest

from checksi.analysis._elemental_values import (
    calculate_elemental_percentages,
    composition_differences,
    exceeds_chn_tolerance,
    extract_composition_entries,
    extract_composition_sections,
    is_two_decimal_percentage,
    parse_composition_values,
)
from checksi.analysis.elemental import check_elemental_analysis, validate_elemental_analysis_values
from checksi.config_schema import AppConfig
from checksi.utils.error_handler import ErrorTracker


def make_config(overrides=None, *, output_mode="essential") -> AppConfig:
    """Use explicit, isolated settings without loading a developer's config."""
    return AppConfig.model_validate({
        "paths": {"input_path": "input.pdf", "output_dir": "."},
        "modes": {"deep_name_analysis": "off", "output_mode": output_mode},
        "solvents": {"entries": [{"name": "CDCl3", "aliases": [], "proton_shifts": [7.26], "carbon_shifts": [77.0]}]},
        "errors": {"default_severity": "critical", "overrides": overrides or {}},
    })


def ethanol_text(calculated="C, 52.14; H, 13.13; O, 34.73", found="C, 52.00; H, 13.00; O, 34.73"):
    """Provide existing semicolon-delimited EA notation."""
    return f"Anal. Calcd for C2H6O: {calculated}. Found: {found}."


class ElementalCharacterizationTests(unittest.TestCase):
    """Protect the established report shape, thresholds and oxygen policy."""

    def evaluate(self, text=None, *, overrides=None, formula="C2H6O"):
        tracker = ErrorTracker()
        result, log = validate_elemental_analysis_values(make_config(overrides), tracker, text or ethanol_text(), formula)
        return result, log, tracker

    def test_theory_uses_average_atomic_weights_and_two_decimal_percentages(self):
        result, log, tracker = self.evaluate()
        # Ethanol: 2*12.011 / (2*12.011 + 6*1.008 + 15.999), etc.
        self.assertTrue(log[0].startswith("Theoretical elemental analysis from formula:"))
        for percentage in ["52.14", "13.13", "34.73"]:
            self.assertIn(percentage, log[0])
        self.assertEqual(result["calculated"], {"C": 52.14, "H": 13.13, "O": 34.73})
        self.assertEqual(result["found"], {"C": 52.0, "H": 13.0, "O": 34.73})
        self.assertEqual(result["matches"], [("C", 0.14), ("H", 0.13), ("O", 0.0)])
        self.assertEqual(list(result), ["calculated", "found", "matches", "warnings", "format_errors", "tolerance_errors"])
        self.assertEqual(sum(tracker.to_dict().values()), 0)

    def test_chn_tolerance_allows_point_four_and_rejects_point_four_one(self):
        for found, count in [("51.74", 0), ("51.73", 1), ("52.54", 0), ("52.55", 1)]:
            with self.subTest(found=found):
                result, _, tracker = self.evaluate(ethanol_text(found=f"C, {found}; H, 13.13; O, 34.73"))
                self.assertEqual(len(result["tolerance_errors"]), count)
                self.assertEqual(tracker.get_count("ea_tolerance_exceeded"), count)

    def test_sulfur_and_oxygen_are_exempt_from_chn_tolerance(self):
        result, _, tracker = self.evaluate(
            "Anal. Calcd for CH4S: C, 24.97; H, 8.38; S, 66.65. Found: C, 24.97; H, 8.38; S, 10.00.",
            formula="CH4S",
        )
        self.assertEqual(result["tolerance_errors"], [])
        self.assertEqual(tracker.get_count("ea_tolerance_exceeded"), 0)

    def test_formula_comparison_preserves_point_zero_one_allowance(self):
        for calculated, count in [("52.15", 0), ("52.16", 1)]:
            with self.subTest(calculated=calculated):
                _, _, tracker = self.evaluate(ethanol_text(calculated=f"C, {calculated}; H, 13.13; O, 34.73"))
                self.assertEqual(tracker.get_count("ea_formula_calcd_mismatch"), count)

    def test_numeric_values_with_wrong_precision_are_excluded_from_matches(self):
        result, log, tracker = self.evaluate(ethanol_text(calculated="C, 52.1; H, 13.13; O, 34.73"))
        self.assertNotIn("C", result["calculated"])
        self.assertNotIn("C", dict(result["matches"]))
        self.assertIn("'C': 52.1", log[1])
        self.assertEqual(tracker.get_count("ea_number_format_invalid"), 1)
        self.assertEqual(tracker.get_count("ea_calcd_found_element_mismatch"), 1)
        self.assertEqual(tracker.get_count("ea_formula_calcd_mismatch"), 1)

    def test_non_numeric_calculated_value_has_existing_numeric_and_format_findings(self):
        result, _, tracker = self.evaluate(ethanol_text(calculated="C, bad; H, 13.13; O, 34.73"))
        self.assertNotIn("C", result["calculated"])
        self.assertEqual(tracker.get_count("ea_number_format_invalid"), 2)
        self.assertEqual(len(result["format_errors"]), 1)
        self.assertEqual(tracker.get_count("ea_formula_element_missing_in_calcd"), 1)

    def test_missing_oxygen_is_required_only_at_warning_or_critical(self):
        for severity, count in [("disabled", 0), ("info", 0), ("warning", 1), ("critical", 1)]:
            with self.subTest(severity=severity):
                result, _, tracker = self.evaluate(
                    ethanol_text(calculated="C, 52.14; H, 13.13", found="C, 52.14; H, 13.13"),
                    overrides={"ea_include_oxygen": severity},
                )
                self.assertEqual(tracker.get_count("ea_formula_element_missing_in_calcd"), count)
                self.assertEqual(len(result["warnings"]), count)
                self.assertEqual(tracker.get_count("ea_include_oxygen"), 0)

    def test_tolerance_diagnostic_severity_only_changes_rendering_and_count(self):
        for severity, prefix, count in [("disabled", None, 0), ("info", "ea_", 1), ("warning", "❗ ", 1), ("critical", "⚠️ ", 1)]:
            with self.subTest(severity=severity):
                result, _, tracker = self.evaluate(ethanol_text(found="C, 51.73; H, 13.13; O, 34.73"), overrides={"ea_tolerance_exceeded": severity})
                self.assertEqual(result["matches"], [("C", 0.41), ("H", 0.0), ("O", 0.0)])
                self.assertEqual(len(result["tolerance_errors"]), count)
                self.assertEqual(tracker.get_count("ea_tolerance_exceeded"), count)
                if prefix:
                    self.assertTrue(result["tolerance_errors"][0].startswith(prefix))

    def test_repeated_calls_do_not_retain_values_or_counters(self):
        first, _, first_tracker = self.evaluate(ethanol_text(found="C, 50.00; H, 13.13; O, 34.73"))
        second, _, second_tracker = self.evaluate()
        self.assertEqual(len(first["tolerance_errors"]), 1)
        self.assertEqual(second["tolerance_errors"], [])
        self.assertEqual(first_tracker.get_count("ea_tolerance_exceeded"), 1)
        self.assertEqual(second_tracker.get_count("ea_tolerance_exceeded"), 0)

    def test_public_check_keeps_tuple_and_detailed_output_contract(self):
        lines = []
        returned, formula, mass = check_elemental_analysis(make_config(output_mode="detailed"), ErrorTracker(), ethanol_text(), lines)
        self.assertIs(returned, lines)
        self.assertEqual(formula, "C2H6O")
        self.assertAlmostEqual(mass, 46.04186481295, places=8)
        self.assertTrue(any(line.startswith("Found vs calculated (EA):") for line in lines))


class ElementalStageTests(unittest.TestCase):
    """Exercise extracted stages independently with small chemistry fixtures."""

    def test_theory_calculation_is_independent_of_text_and_diagnostics(self):
        # Water: 2*1.008 / (2*1.008 + 15.999), rounded to hundredths.
        self.assertEqual(calculate_elemental_percentages("H2O"), {"H": 11.19, "O": 88.81})
        self.assertEqual(calculate_elemental_percentages("C2H6O"), {"C": 52.14, "H": 13.13, "O": 34.73})

    def test_sections_and_entries_retain_reported_element_order(self):
        calculated, found = extract_composition_sections(ethanol_text())
        self.assertEqual(calculated, "C, 52.14; H, 13.13; O, 34.73")
        self.assertEqual(found, "C, 52.00; H, 13.00; O, 34.73")
        self.assertEqual(extract_composition_entries(found), [("C", "52.00"), ("H", "13.00"), ("O", "34.73")])

    def test_ea_found_section_is_independent_of_earlier_hrms_found_mass(self):
        text = "HRMS (ESI) calcd for C2H6O [M]+ 46.0419, found 46.0419. " + ethanol_text()
        _, found = extract_composition_sections(text)
        self.assertEqual(found, "C, 52.00; H, 13.00; O, 34.73")

    def test_parser_separates_numeric_value_from_required_precision(self):
        tracker = ErrorTracker()
        parsed = parse_composition_values(make_config(), tracker, "C, 52.1; H, 13.13", "calculated")
        self.assertEqual(parsed.numeric, {"C": 52.1, "H": 13.13})
        self.assertEqual(parsed.formatted, {"H": 13.13})
        self.assertEqual(tracker.get_count("ea_number_format_invalid"), 1)

    def test_two_decimal_check_rejects_other_numeric_notation(self):
        for value, valid in [("0.00", True), ("52.14", True), ("52", False), ("52.1", False), ("52.140", False), ("-1.00", False), ("nan", False), ("5.214e1", False)]:
            with self.subTest(value=value):
                self.assertEqual(is_two_decimal_percentage(value), valid)

    def test_comparison_stages_apply_existing_rounding_and_stable_order(self):
        self.assertEqual(composition_differences({"H": 13.13, "C": 52.14}, {"C": 52.0, "H": 13.0}), [("C", 0.14), ("H", 0.13)])
        self.assertFalse(exceeds_chn_tolerance("C", 52.14, 51.74))
        self.assertTrue(exceeds_chn_tolerance("N", 10.0, 10.41))
        self.assertFalse(exceeds_chn_tolerance("S", 30.0, 50.0))


if __name__ == "__main__":
    unittest.main()
