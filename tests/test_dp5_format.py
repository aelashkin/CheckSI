"""Score, threshold and skip-reason text shared by reports, logs and the GUI."""

import math
import unittest

from checksi.utils.dp5_format import format_fixed, format_score_and_threshold, format_skip_reasons


class DP5FormatTests(unittest.TestCase):
    def test_fixed_format_never_prints_negative_zero(self):
        self.assertEqual(format_fixed(-2e-15), "0.00")
        self.assertEqual(format_fixed(-0.004), "0.00")
        self.assertEqual(format_fixed(-0.006), "-0.01")
        self.assertEqual(format_fixed(-2.1), "-2.10")

    def test_score_text_agrees_with_the_threshold_comparison(self):
        for score, threshold, expected in [
            (72.756, 10.0, ("72.76", "10.00")),
            (10.0, 10.0, ("10.00", "10.00")),
            (9.99778, 10.0, ("9.998", "10.000")),
            (10.004, 10.0, ("10.004", "10.000")),
            (10.0, 10.004, ("10.000", "10.004")),
            (9.9999999, 10.0, ("9.9999999", "10")),
            (10.0000003, 10.0000004, ("10.0000003", "10.0000004")),
            (0.0, 1e-8, ("0", "1e-08")),
        ]:
            with self.subTest(score=score, threshold=threshold):
                self.assertEqual(format_score_and_threshold(score, threshold), expected)

    def test_score_text_never_states_a_false_bound(self):
        below_ten = math.nextafter(10.0, 0.0)
        for score, threshold in [
            (10.0000003, 10.0000004), (0.0, 1e-8), (9.9999999, 10.0), (below_ten, 10.0),
            (10.0, below_ten), (1e-300, 2e-300), (0.1 + 0.2, 0.3), (72.756, 10.0), (10.0, 10.0),
        ]:
            with self.subTest(score=score, threshold=threshold):
                score_text, threshold_text = format_score_and_threshold(score, threshold)
                shown_score, shown_threshold = float(score_text), float(threshold_text)
                self.assertEqual(shown_score < shown_threshold, score < threshold)
                self.assertEqual(shown_score == shown_threshold, score == threshold)
                self.assertAlmostEqual(shown_score, score, delta=0.005)
                self.assertAlmostEqual(shown_threshold, threshold, delta=0.005)

    def test_skip_reasons_are_listed_most_frequent_first(self):
        self.assertEqual(format_skip_reasons({}), "")
        self.assertEqual(format_skip_reasons(None), "")
        self.assertEqual(
            format_skip_reasons({"structure_unavailable_deep_name_analysis_off": 2, "assignment_ambiguous": 5, "cancelled": 2, "unused": 0}),
            "assignment_ambiguous=5, cancelled=2, structure_unavailable_deep_name_analysis_off=2",
        )


if __name__ == "__main__":
    unittest.main()
