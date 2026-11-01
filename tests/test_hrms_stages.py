"""Exercise HRMS value extraction and arithmetic independently of findings."""

from decimal import Decimal
import unittest

from checksi.analysis.hrms import _extract_formula_token
from checksi.analysis.hrms_ions import extract_ion_fragments
from checksi.analysis.hrms_measurements import extract_reported_masses, mass_error_ppm


class HRMSValueStageTests(unittest.TestCase):
    """Use literal expected fields and exact arithmetic at the numeric boundary."""

    def test_formula_isotope_spans_and_counts_are_preserved(self):
        for token in ["C2H5[2H]", "C2[13C]H6", "C2H5[37Cl]2", "C2H6[34S]"]:
            with self.subTest(token=token):
                self.assertEqual(_extract_formula_token(f"HRMS [M+[13C]H3]+ calcd for {token}+: 66.0045; found 66.0045."), token)
        self.assertIsNone(_extract_formula_token("Exact mass C2H7O+ 47.0491; found 47.0491."))

    def test_ion_fragments_preserve_explicit_isotope_and_complete_composition(self):
        self.assertEqual(extract_ion_fragments("[M+[13C]H3-H2O]+"), [("+", "[13C]H3"), ("-", "H2O")])
        self.assertEqual(extract_ion_fragments("(M–H)-"), [("–", "H")])
        self.assertIsNone(extract_ion_fragments("[M+H++Na]+"))

    def test_reported_fields_return_exact_validated_decimals(self):
        self.assertEqual(extract_reported_masses("calcd for C2H7O+:47.0491; found:47.0492."), (Decimal("47.0491"), Decimal("47.0492")))
        self.assertEqual(extract_reported_masses("calcd 0.0000; found 47.0492."), (None, Decimal("47.0492")))
        self.assertEqual(extract_reported_masses("calcd 47.0491."), (Decimal("47.0491"), None))

    def test_ppm_arithmetic_is_exact_at_and_around_threshold(self):
        for found, expected in [("100.0010", "10"), ("99.9990", "10"), ("100.0011", "11"), ("99.9991", "9")]:
            with self.subTest(found=found):
                self.assertEqual(mass_error_ppm(Decimal("100.0000"), Decimal(found)), Decimal(expected))


if __name__ == "__main__":
    unittest.main()
