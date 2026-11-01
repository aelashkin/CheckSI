"""Regressions for invalid EA formula and measurement control flow."""

import math
import unittest
from unittest.mock import patch

from checksi.analysis._elemental_values import extract_composition_sections, parse_composition_values
from checksi.analysis.elemental import check_elemental_analysis, validate_elemental_analysis_values
from checksi.controller import CheckSIController
from checksi.utils.error_handler import ErrorTracker
from tests.test_elemental import ethanol_text, make_config


class ElementalRegressionTests(unittest.TestCase):
    """Reject invalid fields without hiding later measurements or crashing."""

    def test_invalid_formula_stops_ea_even_when_atom_finding_is_disabled(self):
        for severity, count in [("disabled", 0), ("info", 1), ("warning", 1), ("critical", 1)]:
            with self.subTest(severity=severity):
                cfg = make_config({"formula_atom_symbol_invalid": severity})
                tracker = ErrorTracker()
                lines, formula, mass = check_elemental_analysis(cfg, tracker, "Anal. Calcd for C2Xx6: C, 1.00. Found: C, 1.00.", [])
                self.assertIsNone(formula)
                self.assertIsNone(mass)
                self.assertEqual(len(lines), count)
                self.assertEqual(tracker.get_count("formula_atom_symbol_invalid"), count)

    def test_malformed_found_number_is_not_truncated_or_allowed_to_hide_later_elements(self):
        for token in ["bad", "52", "52.140", "52.14oops", "52.14.9", "-1.00", "nan", "inf", ""]:
            with self.subTest(token=token):
                tracker = ErrorTracker()
                result, _ = validate_elemental_analysis_values(
                    make_config(), tracker, ethanol_text(found=f"C, {token}; H, 13.13; O, 34.73"), "C2H6O",
                )
                self.assertEqual(result["found"], {"H": 13.13, "O": 34.73})
                self.assertEqual(result["matches"], [("H", 0.0), ("O", 0.0)])
                self.assertGreater(tracker.get_count("ea_number_format_invalid"), 0)
                self.assertEqual(tracker.get_count("ea_calcd_found_element_mismatch"), 1)

    def test_missing_calculated_percentage_is_reported_and_later_elements_survive(self):
        tracker = ErrorTracker()
        result, _ = validate_elemental_analysis_values(make_config(), tracker, ethanol_text(calculated="C, ; H, 13.13; O, 34.73"), "C2H6O")
        self.assertEqual(result["calculated"], {"H": 13.13, "O": 34.73})
        self.assertEqual(tracker.get_count("ea_number_format_invalid"), 1)
        self.assertEqual(tracker.get_count("ea_formula_element_missing_in_calcd"), 1)

    def test_duplicate_elements_have_no_unambiguous_comparison_value(self):
        for section in ["calculated", "found"]:
            for repeated in ["52.14", "50.00", "bad"]:
                with self.subTest(section=section, repeated=repeated):
                    tracker = ErrorTracker()
                    values = "C, 52.14; H, 13.13; O, 34.73; C, " + repeated
                    text = ethanol_text(**{section: values})
                    result, log = validate_elemental_analysis_values(make_config(), tracker, text, "C2H6O")
                    self.assertNotIn("C", result[section])
                    self.assertNotIn("C", dict(result["matches"]))
                    self.assertEqual(tracker.get_count("ea_number_format_invalid"), 1)
                    self.assertEqual(tracker.get_count("ea_calcd_found_element_mismatch"), 1)
                    self.assertNotIn("'C':", log[1 if section == "calculated" else 2])

    def test_duplicate_validity_is_independent_of_finding_visibility(self):
        for severity, count in [("disabled", 0), ("info", 1), ("warning", 1), ("critical", 1)]:
            with self.subTest(severity=severity):
                tracker = ErrorTracker()
                parsed = parse_composition_values(make_config({"ea_number_format_invalid": severity}), tracker, "C, 52.14; C, 52.14; C, 52.14; H, 13.13", "found")
                self.assertEqual(parsed.numeric, {"H": 13.13})
                self.assertEqual(parsed.formatted, {"H": 13.13})
                self.assertEqual(len(parsed.format_errors), count)
                self.assertEqual(tracker.get_count("ea_number_format_invalid"), count)

    def test_nonfinite_values_cannot_enter_numeric_formula_comparisons(self):
        for token in ["nan", "inf", "-inf", "9" * 350 + ".00"]:
            with self.subTest(token=token):
                tracker = ErrorTracker()
                parsed = parse_composition_values(make_config(), tracker, f"C, {token}; H, 13.13", "calculated")
                self.assertNotIn("C", parsed.numeric)
                self.assertNotIn("C", parsed.formatted)
                self.assertTrue(all(math.isfinite(value) for value in parsed.numeric.values()))
                self.assertGreater(tracker.get_count("ea_number_format_invalid"), 0)

    def test_percentage_range_rejects_impossible_values_without_rejecting_endpoints(self):
        for token in ["-0.01", "-1.00", "100.01", "152.14"]:
            for severity, count in [("disabled", 0), ("info", 1), ("warning", 1), ("critical", 1)]:
                with self.subTest(token=token, severity=severity):
                    tracker = ErrorTracker()
                    parsed = parse_composition_values(make_config({"ea_number_format_invalid": severity}), tracker, f"C, {token}; H, 13.13", "found")
                    self.assertEqual(parsed.numeric, {"H": 13.13})
                    self.assertEqual(parsed.formatted, {"H": 13.13})
                    self.assertEqual(len(parsed.format_errors), count)
                    self.assertEqual(tracker.get_count("ea_number_format_invalid"), count)
        for token in ["0.00", "100.00"]:
            with self.subTest(token=token):
                tracker = ErrorTracker()
                parsed = parse_composition_values(make_config(), tracker, f"C, {token}", "found")
                self.assertEqual(parsed.numeric, {"C": float(token)})
                self.assertEqual(parsed.formatted, {"C": float(token)})
                self.assertEqual(tracker.get_count("ea_number_format_invalid"), 0)

    def test_found_values_stop_at_sentence_boundary_before_other_chemistry(self):
        text = ethanol_text() + " 1H NMR (400 MHz, CDCl3): 3.50 (s, 2H); 1.00 (t, 3H)."
        _, found = extract_composition_sections(text)
        self.assertEqual(found, "C, 52.00; H, 13.00; O, 34.73")

    def test_calculated_values_survive_when_found_section_is_absent(self):
        for suffix in ["", " The sample was purified; N, 2.00 was noted."]:
            with self.subTest(suffix=suffix):
                tracker = ErrorTracker()
                text = "Anal. Calcd for C2H6O: C, 52.14; H, 13.13." + suffix
                result, _ = validate_elemental_analysis_values(make_config({"ea_include_oxygen": "disabled"}), tracker, text, "C2H6O")
                self.assertEqual(result["calculated"], {"C": 52.14, "H": 13.13})
                self.assertEqual(result["found"], {})
                self.assertEqual(tracker.get_count("ea_calcd_found_element_mismatch"), 2)
                self.assertEqual(tracker.get_count("ea_formula_element_missing_in_calcd"), 0)
                self.assertEqual(tracker.get_count("ea_number_format_invalid"), 0)

    def test_trailing_decimal_before_semicolon_does_not_hide_later_found_value(self):
        tracker = ErrorTracker()
        text = "Anal. Calcd for C2H6O: C, 52.14; H, 13.13. Found: C, 52. ; H, 13.13."
        result, _ = validate_elemental_analysis_values(make_config({"ea_include_oxygen": "disabled"}), tracker, text, "C2H6O")
        self.assertEqual(result["found"], {"H": 13.13})
        self.assertEqual(result["matches"], [("H", 0.0)])
        self.assertEqual(tracker.get_count("ea_number_format_invalid"), 1)
        self.assertEqual(tracker.get_count("ea_calcd_found_element_mismatch"), 1)

    def test_semicolon_adjacent_analysis_section_cannot_become_found_elements(self):
        for following in [
            "1H NMR (400 MHz, CDCl3): 3.50 (s, 2H), 1.20 (s, 3H).",
            "13C NMR (100 MHz, CDCl3): 60.0, 20.0.",
            "13C {1H} NMR (100 MHz, CDCl3): 60.0, 20.0.",
            "13C {1H, 19F} NMR (100 MHz, CDCl3): 60.0, 20.0.",
            "19F {1H}-NMR (376 MHz, CDCl3): -60.0.",
            "HRMS (ESI) calcd for C2H6O [M]+ 46.0419; found 46.0419.",
            "Anal. Calcd for C2H6O: C, 52.14; H, 13.13. Found: C, 52.14; H, 13.13.",
        ]:
            with self.subTest(following=following):
                text = "Anal. Calcd for C2H6O: C, 52.14; H, 13.13. Found: C, 52.14; H, 13.13; " + following
                cfg = make_config({"ea_include_oxygen": "disabled"})
                _, found = extract_composition_sections(text)
                self.assertEqual(found, "C, 52.14; H, 13.13")
                tracker = ErrorTracker()
                result, _ = validate_elemental_analysis_values(cfg, tracker, text, "C2H6O")
                self.assertEqual(result["found"], {"C": 52.14, "H": 13.13})
                self.assertEqual(tracker.get_count("ea_number_format_invalid"), 0)
                self.assertEqual(result["warnings"], [])
                if following.startswith("1H NMR"):
                    public_tracker = ErrorTracker()
                    lines, formula, _ = check_elemental_analysis(cfg, public_tracker, text, [])
                    self.assertEqual(formula, "C2H6O")
                    self.assertEqual(lines, [])
                    self.assertEqual(public_tracker.get_count("ea_number_format_invalid"), 0)

    def test_shared_engine_does_not_report_adjacent_nmr_as_invalid_ea_numbers(self):
        text = "Anal. Calcd for C2H6O: C, 52.14; H, 13.13. Found: C, 52.14; H, 13.13; 1H NMR (400 MHz, CDCl3): δ 3.50 (s, 2H), 1.20 (s, 3H)."
        controller = CheckSIController(make_config({"ea_include_oxygen": "disabled"}))
        with patch("checksi.controller._smiles_and_name_from_paragraph", return_value=(None, None, None, {}, [])):
            result = controller.run_analysis(["Ethanol (1)", text])
        self.assertFalse(result["aborted"])
        self.assertEqual(result["total_found"], 1)
        self.assertEqual(result["error_stats"]["ea_number_format_invalid"], 0)
        self.assertFalse(any("ea_number_format_invalid." in line for line in result["compounds"][0]["all_warnings"]))

    def test_calculated_values_stop_before_next_analysis_when_found_is_absent(self):
        for following in [
            "1H NMR (400 MHz, CDCl3): 3.50 (s, 2H), 1.20 (s, 3H).",
            "HRMS (ESI) calcd for C2H6O [M]+ 46.0419; found 46.0419.",
            "Anal. Calcd for C2H6O: C, 52.14; H, 13.13. Found: C, 52.14; H, 13.13.",
        ]:
            with self.subTest(following=following):
                text = "Anal. Calcd for C2H6O: C, 52.14; H, 13.13; " + following
                calculated, found = extract_composition_sections(text)
                self.assertEqual(calculated, "C, 52.14; H, 13.13")
                self.assertIsNone(found)
                tracker = ErrorTracker()
                result, _ = validate_elemental_analysis_values(make_config({"ea_include_oxygen": "disabled"}), tracker, text, "C2H6O")
                self.assertEqual(result["calculated"], {"C": 52.14, "H": 13.13})
                self.assertEqual(result["found"], {})
                self.assertEqual(tracker.get_count("ea_number_format_invalid"), 0)
                self.assertEqual(tracker.get_count("ea_calcd_found_element_mismatch"), 2)

    def test_hrms_marker_substrings_in_bad_value_do_not_hide_later_ea_values(self):
        for token in ["resin", "residue", "Mesityl"]:
            with self.subTest(token=token):
                text = f"Anal. Calcd for C2H6O: C, 52.14; H, 13.13. Found: C, {token}; H, 13.13."
                tracker = ErrorTracker()
                result, _ = validate_elemental_analysis_values(make_config({"ea_include_oxygen": "disabled"}), tracker, text, "C2H6O")
                self.assertEqual(result["found"], {"H": 13.13})
                self.assertEqual(tracker.get_count("ea_number_format_invalid"), 2)

    def test_duplicate_then_clean_parse_has_no_retained_invalid_state(self):
        tracker = ErrorTracker()
        first = parse_composition_values(make_config(), tracker, "C, 52.14; C, 52.14", "calculated")
        second = parse_composition_values(make_config(), tracker, "C, 52.14", "calculated")
        self.assertEqual(first.formatted, {})
        self.assertEqual(second.formatted, {"C": 52.14})
        self.assertEqual(tracker.get_count("ea_number_format_invalid"), 1)


if __name__ == "__main__":
    unittest.main()
