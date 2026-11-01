"""Small-text regressions for the supplied PR 13 elemental-analysis review."""

import unittest

from checksi.analysis._elemental_values import parse_composition_values
from checksi.analysis.elemental import check_elemental_analysis, validate_elemental_analysis_values
from checksi.analysis.hrms import check_hrms
from checksi.analysis.hrms_fields import find_hrms_formula
from checksi.utils.error_handler import ErrorTracker
from tests.test_elemental import make_config


def amine_text(found="C, 81.70; H, 9.20; N, 9.01", *, header=":"):
    """Use the reviewed C21H28N2 entry with independently rounded percentages."""
    return f"Anal. Calcd for C21H28N2{header} C, 81.77; H, 9.15; N, 9.08. Found: {found}"


class ElementalReviewBoundaryTests(unittest.TestCase):
    """Keep valid EA fields separate from following properties and header prose."""

    def test_property_tails_do_not_become_found_element_records(self):
        for tail in ["mp 120-122 °C.", "IR (neat) 1600 cm-1.", "MS (EI) m/z 308.", "ee 98%.", "Rf = 0.35.", "Et 3.10 g."]:
            with self.subTest(tail=tail):
                text = amine_text() + "; " + tail
                tracker = ErrorTracker()
                result, _ = validate_elemental_analysis_values(make_config(), tracker, text, "C21H28N2")
                self.assertEqual(result["found"], {"C": 81.70, "H": 9.20, "N": 9.01})
                self.assertEqual(result["matches"], [("C", 0.07), ("H", 0.05), ("N", 0.07)])
                self.assertEqual(sum(tracker.to_dict().values()), 0)
                public_tracker = ErrorTracker()
                lines, formula, _ = check_elemental_analysis(make_config(), public_tracker, text, [])
                self.assertEqual(formula, "C21H28N2")
                self.assertEqual(lines, [])

    def test_glued_period_tail_preserves_last_value_and_real_tolerance_finding(self):
        for tail in ["IR (neat) 1600 cm-1.", "(IR, neat) 1600 cm-1.", "s"]:
            with self.subTest(tail=tail):
                tracker = ErrorTracker()
                text = amine_text(found="C, 81.70; H, 9.20; N, 8.01") + "." + tail
                result, _ = validate_elemental_analysis_values(make_config(), tracker, text, "C21H28N2")
                self.assertEqual(result["found"], {"C": 81.70, "H": 9.20, "N": 8.01})
                self.assertEqual(tracker.get_count("ea_tolerance_exceeded"), 1)
                self.assertEqual(tracker.get_count("ea_number_format_invalid"), 0)
                self.assertEqual(tracker.get_count("ea_calcd_found_element_mismatch"), 0)
                self.assertIn("1.07%", result["tolerance_errors"][0])

    def test_header_abbreviations_do_not_end_calculated_values_before_the_first_record(self):
        for header in [" (M.W. 308.47):", " (cf. ref. 12):", "."]:
            with self.subTest(header=header):
                tracker = ErrorTracker()
                text = amine_text(found="C, 81.70; H, 9.80; N, 9.01.", header=header)
                result, _ = validate_elemental_analysis_values(make_config(), tracker, text, "C21H28N2")
                self.assertEqual(result["calculated"], {"C": 81.77, "H": 9.15, "N": 9.08})
                self.assertEqual(tracker.get_count("ea_tolerance_exceeded"), 1)
                self.assertEqual(tracker.get_count("ea_number_format_invalid"), 0)
                self.assertEqual(tracker.get_count("ea_calcd_found_element_mismatch"), 0)
                self.assertEqual(tracker.get_count("ea_formula_element_missing_in_calcd"), 0)
                self.assertIn("0.65%", result["tolerance_errors"][0])

    def test_malformed_real_element_values_survive_property_tail_bounding(self):
        tracker = ErrorTracker()
        text = amine_text(found="C, bad; H, 9.15; N, 9.08; mp 120-122 °C.")
        result, _ = validate_elemental_analysis_values(make_config(), tracker, text, "C21H28N2")
        self.assertEqual(result["found"], {"H": 9.15, "N": 9.08})
        self.assertEqual(tracker.get_count("ea_number_format_invalid"), 2)
        self.assertEqual(tracker.get_count("ea_calcd_found_element_mismatch"), 1)


class ElementalReviewMalformedTests(unittest.TestCase):
    """Malformed records stay invalid and visible while later fields survive."""

    def test_malformed_element_records_have_precise_findings_at_each_severity(self):
        for entry in ["H: 9.80", "H=9.80", "H:9.80", "Xx, 1.00"]:
            for severity, prefix in [("disabled", None), ("info", ""), ("warning", "❗ "), ("critical", "⚠️ ")]:
                with self.subTest(entry=entry, severity=severity):
                    tracker = ErrorTracker()
                    parsed = parse_composition_values(make_config({"ea_number_format_invalid": severity}), tracker, f"C, 81.77; {entry}; N, 9.08", "found")
                    self.assertEqual(parsed.numeric, {"C": 81.77, "N": 9.08})
                    self.assertEqual(parsed.formatted, {"C": 81.77, "N": 9.08})
                    expected = [] if prefix is None else [f"{prefix}ea_number_format_invalid. Malformed entry in found composition of elemental analysis: '{entry}'. Expected an element symbol followed by a percentage."]
                    self.assertEqual(parsed.format_errors, expected)
                    self.assertEqual(parsed.warnings, [])
                    self.assertEqual(tracker.get_count("ea_number_format_invalid"), len(expected))

    def test_unrecognized_header_residue_is_reported_without_reinterpreting_the_first_field(self):
        for residue in ["(308.47):", "·0.5H2O:", "(%):", ".1H26N2:"]:
            with self.subTest(residue=residue):
                text = f"Anal. Calcd for C21H28N2 {residue} C, 81.77; H, 9.15; N, 9.08. Found: C, 81.77; H, 9.15; N, 9.08."
                tracker = ErrorTracker()
                result, _ = validate_elemental_analysis_values(make_config(), tracker, text, "C21H28N2")
                self.assertEqual(result["calculated"], {"H": 9.15, "N": 9.08})
                self.assertEqual(tracker.get_count("ea_number_format_invalid"), 1)
                self.assertEqual(result["format_errors"], [f"⚠️ ea_number_format_invalid. Malformed entry in calculated composition of elemental analysis: '{residue} C, 81.77'. Expected an element symbol followed by a percentage."])

    def test_property_tail_ends_collection_instead_of_resuming_at_later_element_like_text(self):
        parsed = parse_composition_values(make_config(), ErrorTracker(), "C, 81.77; mp 120-122 °C; H, 9.15", "found")
        self.assertEqual(parsed.formatted, {"C": 81.77})
        self.assertEqual(parsed.warnings, [])
        self.assertEqual(parsed.format_errors, [])

    def test_real_elements_with_property_like_symbols_are_preserved(self):
        parsed = parse_composition_values(make_config(), ErrorTracker(), "Ir, 52.14; Rf, 47.86", "found")
        self.assertEqual(parsed.formatted, {"Ir": 52.14, "Rf": 47.86})
        self.assertEqual(parsed.format_errors, [])


class ElementalReviewDiagnosticTests(unittest.TestCase):
    """Assert rendered variants and exact counters rather than mere nonzero counts."""

    def test_nonfinite_values_use_the_numeric_variant_and_exact_format_counts(self):
        for token in ["nan", "inf", "-inf", "9" * 350 + ".00"]:
            for severity, prefix in [("disabled", None), ("info", ""), ("warning", "❗ "), ("critical", "⚠️ ")]:
                with self.subTest(token=token, severity=severity):
                    tracker = ErrorTracker()
                    parsed = parse_composition_values(make_config({"ea_number_format_invalid": severity}), tracker, f"C, {token}; H, 9.15", "calculated")
                    self.assertEqual(parsed.numeric, {"H": 9.15})
                    self.assertEqual(parsed.formatted, {"H": 9.15})
                    expected_warnings = [] if prefix is None else [f"{prefix}ea_number_format_invalid. Skipping non-numeric value for C: {token}."]
                    expected_format = [] if prefix is None or token.endswith(".00") else [f"{prefix}ea_number_format_invalid. Invalid number format in calculated composition (C) of elemental analysis: '{token}' (should be XX.XX)."]
                    self.assertEqual(parsed.warnings, expected_warnings)
                    self.assertEqual(parsed.format_errors, expected_format)
                    self.assertEqual(tracker.get_count("ea_number_format_invalid"), len(expected_warnings) + len(expected_format))

    def test_each_percentage_template_renders_all_of_its_placeholders(self):
        for entry, expected in [
            ("C, 81.77; C, 81.77", "Element C occurs more than once in found composition of elemental analysis; no unique percentage can be used."),
            ("H,", "Missing percentage for H in found composition of elemental analysis."),
            ("N, 100.01", "Percentage for N in found composition of elemental analysis is outside 0–100%: 100.01."),
        ]:
            with self.subTest(entry=entry):
                tracker = ErrorTracker()
                parsed = parse_composition_values(make_config(), tracker, entry, "found")
                self.assertEqual(parsed.format_errors, ["⚠️ ea_number_format_invalid. " + expected])
                self.assertEqual(parsed.warnings, [])
                self.assertEqual(tracker.get_count("ea_number_format_invalid"), 1)


class ElementalReviewSharedFormulaTests(unittest.TestCase):
    """Use the same HRMS ownership and independent findings as the shared chemistry."""

    def test_prose_instrument_marker_cannot_claim_a_formula_after_nmr(self):
        text = "Colourless oil (see ESI). 1H NMR (400 MHz, CDCl3): δ 1.00 (s, 6H); formula C2H6O."
        self.assertIsNone(find_hrms_formula(text))
        tracker = ErrorTracker()
        lines, formula, mass = check_elemental_analysis(make_config(), tracker, text, [])
        self.assertIsNone(formula)
        self.assertEqual(mass, 0)
        self.assertEqual(tracker.get_count("formula_not_found"), 1)
        self.assertEqual(len(lines), 1)
        self.assertIn("formula_not_found.", lines[0])

    def test_mid_formula_isotopes_and_leading_label_scope_match_shared_identifier(self):
        for formula, identified in [("C5[13C]H13O2+", True), ("[13C]6H6", True), ("C[13C]H5O2+", True), ("[13C]6 H6", False)]:
            with self.subTest(formula=formula):
                text = f"HRMS (ESI) calcd for {formula} [M+H]+ 118.0950; found 118.0950."
                self.assertEqual(find_hrms_formula(text) is not None, identified)
                tracker = ErrorTracker()
                lines, ea_formula, _ = check_elemental_analysis(make_config(), tracker, text, [])
                self.assertIsNone(ea_formula)
                self.assertEqual(tracker.get_count("formula_not_found"), 0)
                lines, hrms_formula, mass = check_hrms(make_config(), tracker, text, lines)
                self.assertEqual(tracker.get_count("formula_invalid_format"), 0)
                if not identified:
                    self.assertIsNone(hrms_formula)
                    self.assertFalse(mass)
                    refusal, = [line for line in lines if 'not checked by CheckSI' in line]
                    self.assertIn(formula.removesuffix('+'), refusal)
                else:
                    self.assertEqual(hrms_formula, formula.removesuffix('+'))

    def test_formula_findings_remain_separate_report_lines_with_independent_visibility(self):
        text = "Anal. Calcd for C2OH6H: C, 50.00; H, 10.00; O, 40.00. Found: C, 50.00; H, 10.00; O, 40.00."
        for severity, duplicate_count in [("disabled", 0), ("info", 1), ("warning", 1), ("critical", 1)]:
            with self.subTest(severity=severity):
                tracker = ErrorTracker()
                lines, _, _ = check_elemental_analysis(make_config({"formula_duplicate_elements": severity}), tracker, text, [])
                formula_findings = [line for line in lines if "formula_duplicate_elements." in line or "formula_order_invalid." in line]
                self.assertEqual(len(formula_findings), duplicate_count + 1)
                self.assertTrue(all("\n" not in line for line in formula_findings))
                self.assertEqual(tracker.get_count("formula_duplicate_elements"), duplicate_count)
                self.assertEqual(tracker.get_count("formula_order_invalid"), 1)


if __name__ == "__main__":
    unittest.main()
