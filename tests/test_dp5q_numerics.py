"""Independent arithmetic and fit fixtures for the corrected DP5q score."""

import math
import unittest

import numpy as np
from scipy.special import ndtri
from scipy.stats import linregress

from checksi.dp5q.scoring import ScoringError, aggregate_compatibility, diagnostic_errors, fit_gaussian


class DP5qNumericsTests(unittest.TestCase):
    def test_scaled_errors_match_independent_linear_regressions_and_sign(self):
        observed = np.array([129., 128., 21., 138., 125.])
        predicted = np.array([[128.8, 128.1, 21.2, 138.0, 125.1],
                              [129.2, 127.8, 20.9, 138.3, 124.9]])
        weights = np.array([.3, .7])
        corrected = []
        for values in predicted:
            regression = linregress(observed, values)
            corrected.append((values-regression.intercept)/regression.slope-observed)
        expected = np.array(corrected)
        actual = diagnostic_errors(predicted, observed, weights)
        np.testing.assert_allclose(actual["scaled_errors"], weights @ expected, atol=1e-12, rtol=0)
        self.assertAlmostEqual(actual["corrected_mae"], weights @ np.abs(expected).mean(axis=1), places=12)

    def test_normal_quantiles_recover_location_and_width(self):
        levels = np.arange(1, 100) / 100
        fit = fit_gaussian(100 + 3.5 * ndtri(levels), levels)
        self.assertAlmostEqual(fit.mu, 100, places=8)
        self.assertAlmostEqual(fit.sigma, 3.5, places=8)
        self.assertLess(fit.residual_rms, 1e-10)

    def test_location_and_scale_transform_preserve_fit(self):
        levels = np.arange(1, 100) / 100
        fit = fit_gaussian(-30 + 0.002 * ndtri(levels), levels)
        self.assertAlmostEqual(fit.mu, -30, places=10)
        self.assertAlmostEqual(fit.sigma, 0.002, places=10)

    def test_flat_nonfinite_and_invalid_levels_are_not_scored(self):
        levels = np.arange(1, 100) / 100
        for values, quantiles in ((np.ones(99), levels), (np.full(99, np.nan), levels),
                                  (ndtri(levels), np.ones(99))):
            with self.subTest(), self.assertRaises(ScoringError):
                fit_gaussian(values, quantiles)

    def test_center_score_is_exactly_one_without_epsilon(self):
        result = aggregate_compatibility([100], [[100]], [[2]], [1])
        self.assertEqual(result.score, 1.0)
        self.assertEqual(result.log_score, 0.0)

    def test_extreme_tail_has_no_artificial_floor(self):
        observed = np.zeros(30)
        observed[-1] = 12
        result = aggregate_compatibility(observed, np.zeros((1, 30)), np.ones((1, 30)), [1])
        expected = math.exp(math.log(math.erfc(12 / math.sqrt(2))) / 30)
        self.assertAlmostEqual(result.score, expected, places=12)
        self.assertLess(result.score, 0.1)
        self.assertAlmostEqual(math.exp(math.log(1e-6) / 30), 0.63095734448, places=10)

    def test_mixture_weights_compatibilities_not_fit_parameters(self):
        result = aggregate_compatibility([0], [[0], [4]], [[1], [1]], [0.25, 0.75])
        expected = 0.25 + 0.75 * math.erfc(4 / math.sqrt(2))
        self.assertAlmostEqual(result.score, expected, places=12)

    def test_large_residual_retains_finite_log_score_after_display_underflow(self):
        result = aggregate_compatibility([100], [[0]], [[1]], [1])
        self.assertEqual(result.score, 0.0)
        self.assertTrue(math.isfinite(result.log_score))
        self.assertLess(result.log_score, -5000)

    def test_invalid_sigma_or_populations_never_become_a_score(self):
        for sigmas, weights in (([[0]], [1]), ([[1]], [0]), ([[1]], [2]), ([[math.nan]], [1])):
            with self.subTest(), self.assertRaises(ScoringError):
                aggregate_compatibility([0], [[0]], sigmas, weights)

    def test_singular_regression_is_a_diagnostic_failure_only(self):
        result = diagnostic_errors([[10, 20]], [15, 15], [1])
        self.assertEqual(result["raw_mae"], 5)
        self.assertIsNone(result["corrected_mae"])
        self.assertEqual(result["regression_reason"], "constant_experimental_shifts")


if __name__ == "__main__":
    unittest.main()
