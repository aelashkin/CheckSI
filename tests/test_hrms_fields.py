"""Formula-span ownership without expanding the established token start."""

import unittest

from checksi.analysis.hrms_fields import find_hrms_formula


class HRMSFieldTests(unittest.TestCase):
    """Retain original spans, isolate sections and recover unmatched brackets."""

    def test_mid_formula_labels_and_original_span(self):
        text = "HRMS (ESI) calcd for C5[13C]H13O2+ [M+[13C]H3+C2H3N]+ 118.0960, found 118.0960."
        result = find_hrms_formula(text)
        self.assertEqual(result.formula, "C5[13C]H13O2")
        self.assertEqual(text[result.formula_start:result.formula_end], result.formula)
        self.assertEqual(text[result.header_start:result.header_end], "ESI")

    def test_prose_or_lrms_before_nmr_cannot_own_later_formula(self):
        for prefix in ("Colourless oil (see ESI). ", "MS (ESI) m/z 310 [M+H]+; "):
            with self.subTest(prefix=prefix):
                text = prefix + "13C NMR (101 MHz, CDCl3) δ 77.0. HRMS (ESI) calcd for C21H29N2+ 309.2325, found 309.2418."
                result = find_hrms_formula(text)
                self.assertEqual(result.formula, "C21H29N2")
                self.assertGreater(result.header_start, text.index("HRMS"))

    def test_hresims_after_ea_keeps_original_instrument_family(self):
        text = "Anal. Calcd for C2H6O: C, 52.14. Found: C, 52.14. HRESIMS calcd for C2H7O+ 47.0491, found 47.0501."
        self.assertEqual(find_hrms_formula(text).formula, "C2H7O")

    def test_nested_isotope_adduct_does_not_supply_the_formula(self):
        text = "HRMS (ESI) [M+[13C]H3+C2H3N]+ calcd for C10H15NO2+ 184.1125, found 184.1125."
        self.assertEqual(find_hrms_formula(text).formula, "C10H15NO2")

    def test_unclosed_brackets_do_not_swallow_calculated_formula(self):
        for prefix in ("[M + H+ ", "[[M + H+ "):
            with self.subTest(prefix=prefix):
                self.assertEqual(find_hrms_formula("HRMS (ESI) " + prefix + "Calcd for C21H29N2 309.2480; Found 309.2418.").formula, "C21H29N2")

    def test_spaced_leading_label_forms_remain_unsupported(self):
        for formula in ("[13C]6 H6", "C [13C]H5O"):
            with self.subTest(formula=formula):
                self.assertIsNone(find_hrms_formula("HRMS (ESI) calcd for " + formula + " 47.0491, found 47.0491."))
