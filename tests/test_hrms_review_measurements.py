"""Accepted mass-field forms, field ownership, and bounded parsing regressions."""

from decimal import Decimal
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from checksi import file_io
from checksi.analysis.hrms import check_hrms
from checksi.analysis.hrms_measurements import extract_reported_masses
from checksi.utils.error_handler import ErrorTracker
from tests.test_hrms import make_config


class AcceptedMassFieldTests(unittest.TestCase):
    """Retain accepted separators without borrowing an unrelated later number."""

    def assert_real_ppm_finding(self, text):
        tracker = ErrorTracker()
        output, _, _ = check_hrms(make_config(), tracker, text, [])
        self.assertEqual(tracker.get_count("hrms_calcd_mass_missing"), 0, output)
        self.assertEqual(tracker.get_count("hrms_found_mass_missing"), 0, output)
        self.assertEqual(tracker.get_count("hrms_ppm_too_large"), 2, output)

    def test_found_prefixes_and_trailing_punctuation_preserve_ppm_validation(self):
        for found in [
            "found 47.0501.;", "found 47.0501.,", "found 47.0501.)", "found 47.0501.:",
            "found m/z 47.0501", "found: m/z 47.0501", "found [M+H]+ 47.0501",
            "found, 47.0501", "found (47.0501)", "found – 47.0501", "found47.0501",
        ]:
            with self.subTest(found=found):
                self.assert_real_ppm_finding(f"HRMS C2H7O+ [M+H]+ 47.0491, {found}")

    def test_charge_adjacent_calculated_mass_is_still_the_calculated_field(self):
        self.assert_real_ppm_finding("HRMS calcd for C2H7O+ [M+H]+47.0491, found 47.0501.")

    def test_real_file_io_cleanup_punctuation_does_not_hide_mass(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "input.pdf"
            source.touch()
            for punctuation in [";", ",", ")", ":"]:
                text = f"HRMS C2H7O+ [M+H]+ 47.0491, found 47.0501{punctuation} IR (neat) 1600"
                with self.subTest(punctuation=punctuation), patch.object(file_io, "extract_text_from_pdf", return_value=text):
                    paragraphs = file_io.paragraphs_from_input(str(source), header_margin=0.1, footer_margin=0.1)
                    hrms = next(paragraph for paragraph in paragraphs if "HRMS" in paragraph)
                    self.assert_real_ppm_finding(hrms)

    def test_optional_label_does_not_let_later_numbers_repair_a_bad_field(self):
        for found in ["found m/z nope; 47.0501", "found [M+H]+ nope; 47.0501", "found (47.0501.2)"]:
            with self.subTest(found=found):
                calculated, actual = extract_reported_masses(f"47.0491, {found}")
                self.assertEqual(calculated, Decimal("47.0491"))
                self.assertIsNone(actual)


class OwnedMassFieldTests(unittest.TestCase):
    """Use the same analytical header that owns the identified formula."""

    assert_real_ppm_finding = AcceptedMassFieldTests.assert_real_ppm_finding

    def test_prose_esi_and_low_resolution_ms_before_nmr_do_not_own_hrms_masses(self):
        for prefix in [
            "Colourless oil (see ESI). 13C NMR (101 MHz, CDCl3): δ 77.0.",
            "MS (ESI) m/z 310 [M+H]+; 13C NMR (101 MHz, CDCl3): δ 77.0.",
        ]:
            with self.subTest(prefix=prefix):
                self.assert_real_ppm_finding(f"{prefix} HRMS (ESI) calcd for C2H7O+ [M+H]+ 47.0491, found 47.0501.")

    def test_existing_compact_instrument_headers_after_ea_own_their_mass_fields(self):
        for header in ["HRESIMS", "HR-ESIMS"]:
            with self.subTest(header=header):
                self.assert_real_ppm_finding(
                    "Anal. Calcd for C2H6O: C, 52.14; H, 13.13. Found: C, 52.14; H, 13.13. "
                    f"{header} calcd for C2H7O+ [M+H]+ 47.0491, found 47.0501.",
                )


class IndependentMassFieldTests(unittest.TestCase):
    """An unsupported observed label cannot replace the calculated mass."""

    def test_observed_label_leaves_real_calculated_value_and_missing_found(self):
        for label in ["observed", "measured", "obsd", "exp.", "experimental"]:
            with self.subTest(label=label):
                text = f"HRMS C2H7O+ [M+H]+ calcd 47.0491, {label} 47.0499."
                self.assertEqual(extract_reported_masses(text), (Decimal("47.0491"), None))
                tracker = ErrorTracker()
                output, _, _ = check_hrms(make_config(), tracker, text, [])
                self.assertEqual(tracker.get_count("calcd_formula_mass_mismatch"), 0, output)
                self.assertEqual(tracker.get_count("hrms_found_mass_missing"), 1)

    def test_multiple_unlabelled_values_without_found_are_ambiguous(self):
        self.assertEqual(extract_reported_masses("47.0491, 47.0499."), (None, None))

    def test_observed_only_value_is_not_a_calculated_value(self):
        self.assertEqual(extract_reported_masses("HRMS C2H7O+ [M+H]+ observed 47.0499."), (None, None))


class BoundedMassParsingTests(unittest.TestCase):
    """A whitespace-only field terminates rather than backtracking quadratically."""

    def test_long_whitespace_after_found_terminates_in_an_isolated_process(self):
        script = (
            "from checksi.analysis.hrms_measurements import extract_reported_masses; "
            "assert extract_reported_masses('47.0491, found' + ' ' * 60000)[1] is None"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=8, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
