"""Small NMR cases that characterize the accepted candidate grammar."""

import unittest

from checksi.analysis.nmr import (
    _extract_nmr_section,
    calculate_nmr_number_count,
    check_nmr,
    check_presence_nmr_spectra,
    preprocess_signals,
)
from checksi.config_schema import AppConfig
from checksi.analysis.structure_analysis import C13Alternative
from checksi.utils.error_handler import ErrorTracker


class NMRTests(unittest.TestCase):
    """Check sections, counts, findings and independent counters directly."""

    def setUp(self):
        self.cfg = AppConfig.model_validate({
            "paths": {"input_path": "input.pdf", "output_dir": "."},
            "modes": {"deep_name_analysis": "off", "output_mode": "essential"},
            "solvents": {"entries": [{"name": "CDCl3", "aliases": ["chloroform-d"], "proton_shifts": [7.26], "carbon_shifts": [77.0]}]},
            "errors": {"default_severity": "warning", "overrides": {}},
        })

    def carbon(self, text, tracker=None, **kwargs):
        return check_nmr(self.cfg, tracker or ErrorTracker(), text, "13C", 1, kwargs.pop("trees_count", None), 400, 0, 0, kwargs.pop("formula", None), 0, 0, **kwargs)

    def test_sections_stop_before_neighbor_spectra_and_keep_decoupling_flag(self):
        text = "1H NMR (400 MHz, CDCl3): δ 1.00 (s, 6H); 13C{1H, 19F} NMR (100 MHz, CDCl3): δ 60.0, 20.0. HRMS data."
        proton, _ = _extract_nmr_section(text, "1H")
        carbon, decoupled = _extract_nmr_section(text, "13C")
        self.assertEqual(proton.strip(), "(400 MHz, CDCl3): δ 1.00 (s, 6H)")
        self.assertEqual(carbon.strip(), "(100 MHz, CDCl3): δ 60.0, 20.0")
        self.assertEqual(decoupled, 1)
        self.assertEqual(_extract_nmr_section("No spectrum", "13C"), (None, 0))

    def test_spaced_and_cyrillic_carbon_counts_preserve_observation_and_atom_distinction(self):
        for signals in ["60.0 (2C), 20.0", "60.0 (2 C), 20.0", "60.0 (2С), 20.0"]:
            with self.subTest(signals=signals):
                count = calculate_nmr_number_count(self.cfg, ErrorTracker(), signals, "13C", 1, 1, None, 0, 0)[0]
                expanded = preprocess_signals(signals)
                self.assertEqual(count, 3)
                self.assertEqual(expanded, [60.0, 60.0, 20.0])
                self.assertEqual(len(expanded), 3)

    def test_ranges_keep_candidate_first_endpoint_and_negative_shift_policy(self):
        for text, expected in [("130.0-131.0", [130.0]), ("130.0 – 131.0", [130.0]), ("-1.0 - -2.0", [-1.0])]:
            with self.subTest(text=text):
                self.assertEqual(preprocess_signals(text), expected)

    def test_missing_and_empty_carbon_sections_have_safe_dp5_defaults(self):
        for text, key in [("No spectrum.", "c13_section_missing"), ("13C NMR (100 MHz, CDCl3): δ .", "nmr_section_missing")]:
            with self.subTest(text=text):
                tracker = ErrorTracker()
                result = self.carbon(text, tracker)
                self.assertFalse(result[6])
                self.assertIsNone(result[7])
                self.assertEqual(tracker.get_count(key), 1)
                self.assertTrue(any(key + "." in line for line in result[0]))

    def test_missing_delta_does_not_fabricate_a_signal_list(self):
        result = self.carbon("13C NMR (100 MHz, CDCl3): 60.0, 20.0.")
        self.assertFalse(result[6])
        self.assertIsNone(result[7])

    def test_malformed_separator_emits_finding_and_counter(self):
        tracker = ErrorTracker()
        result = self.carbon("13C NMR (100 MHz, CDCl3): δ 60.0 20.0.", tracker)
        self.assertEqual(tracker.get_count("nmr_signal_format"), 1)
        self.assertTrue(any(line.startswith("❗ nmr_signal_format.") for line in result[0]))

    def test_order_duplicate_and_precision_findings_are_counted_once(self):
        tracker = ErrorTracker()
        result = calculate_nmr_number_count(self.cfg, tracker, "20.0, 60.0, 20.0, 5", "13C", 1, 1, None, 0, 0)
        self.assertEqual(result[0], 3)
        for key in ["nmr_signal_order_invalid", "xnuc_duplicate_signals", "nmr_signal_precision_invalid"]:
            self.assertEqual(tracker.get_count(key), 1)
            self.assertTrue(any(key + "." in str(item) for item in result))

    def test_solvent_peak_is_retained_in_candidate_dp5_shifts(self):
        result = self.carbon("13C NMR (100 MHz, chloroform-d): δ 77.0, 20.0.", trees_count=2, carbon_groups=[])
        self.assertEqual(result[7], [77.0, 20.0])
        self.assertTrue(result[6])
        self.assertTrue(any("solvent_carbon_peak_found." in line for line in result[0]))

    def test_required_nucleus_and_structural_mismatch_are_explicit(self):
        tracker = ErrorTracker()
        findings = check_presence_nmr_spectra(self.cfg, tracker, ["C", "H", "F"], "13C NMR", [])
        self.assertEqual(tracker.get_count("required_nmr_missing"), 1)
        self.assertIn("F found", findings[0])
        result = self.carbon("13C NMR (100 MHz, CDCl3): δ 60.0, 20.0.", tracker, trees_count=3, carbon_groups=[])
        self.assertFalse(result[6])
        self.assertEqual(tracker.get_count("c13_count_structure_check"), 1)

    def test_lower_stereo_alternatives_do_not_replace_the_full_partition(self):
        alternatives = [
            C13Alternative("first", 2, [["C1", "C2"]]),
            C13Alternative("second", 2, [["C2", "C3"]]),
        ]
        result = self.carbon(
            "13C NMR (100 MHz, CDCl3): δ 60.0, 20.0.",
            trees_count=3,
            c13_count_alternatives_w_groups=alternatives,
            stereogenic_signal_flag=True,
        )
        self.assertFalse(result[6])
        self.assertEqual(result[5], [])
        self.assertEqual(result[7], [60., 20.])
        self.assertTrue(any("c13_count_structure_check_stereo." in line for line in result[0]))

    def test_proton_integrals_compare_to_formula_and_repeated_runs_stay_independent(self):
        for _ in range(2):
            tracker = ErrorTracker()
            result = check_nmr(self.cfg, tracker, "1H NMR (400 MHz, CDCl3): δ 3.50 (s, 2H), 1.20 (s, 3H).", "1H", 0, None, 0, 0, 0, "C2H6", 0, 0)
            self.assertEqual(tracker.get_count("h_count_mismatch"), 1)
            self.assertTrue(any("h_count_mismatch." in line for line in result[0]))
