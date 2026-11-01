"""Numerical and type regressions found by independent checkpoint review."""

import unittest

import numpy as np
from scipy.special import ndtri

from checksi.dp5q.contracts import CheckRequest, CheckResult, decode_frame
from checksi.dp5q.scoring import ScoringError, fit_gaussian


class IdentifiabilityTests(unittest.TestCase):
    def test_half_folded_quantiles_cannot_use_the_constant_cdf_boundary(self):
        levels = np.arange(1, 100) / 100
        values = ndtri(levels)
        values[:50] *= -1
        for offset, scale in ((0., 1.), (120., 2.), (-10., .002)):
            with self.subTest(offset=offset, scale=scale):
                with self.assertRaises(ScoringError) as caught:
                    fit_gaussian(offset + scale * values, levels)
                self.assertEqual(caught.exception.reason, "unidentifiable_distribution")

    def test_reversed_quantiles_cannot_become_an_infinitely_wide_perfect_match(self):
        levels = np.arange(1, 100) / 100
        with self.assertRaises(ScoringError) as caught:
            fit_gaussian(-ndtri(levels), levels)
        self.assertEqual(caught.exception.reason, "unidentifiable_distribution")

    def test_small_crossings_are_diagnostics_not_a_blanket_refusal(self):
        levels = np.arange(1, 100) / 100
        values = ndtri(levels)
        values[49], values[50] = values[50], values[49]
        fit = fit_gaussian(values, levels)
        self.assertEqual(fit.crossing_count, 1)
        self.assertGreater(fit.sigma, 0)

    def test_boolean_result_values_and_assignment_indices_are_not_numbers(self):
        for field in ("score_raw", "log_score"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                CheckResult(compound_id="1", status="assessed", **{"score_raw": 1.0, field: True})
        for fixed in ({0: True}, {True: 0}):
            with self.subTest(fixed=fixed), self.assertRaises(ValueError):
                CheckRequest(compound_id="1", smiles="CC", carbon_shifts=[1, 2], fixed_assignments=fixed)

    def test_frame_to_result_rejects_string_and_boolean_numbers(self):
        for value in (True, False, "0.8"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                CheckResult.model_validate({"compound_id": "1", "status": "assessed", "score_raw": value})
        request = CheckRequest(compound_id="1", smiles="CC", carbon_shifts=[1, 2], fixed_assignments={"0": 1})
        self.assertEqual(request.fixed_assignments, {0: 1})
        with self.assertRaises(ValueError):
            decode_frame(b'{"kind":"check","run_id":"r","run_id":"other","generation":1,"sequence":1}\n')


if __name__ == "__main__":
    unittest.main()
