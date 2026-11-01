"""Frozen independent G03 vectors for weighted evidence and separate distributions."""

import json
import math
from pathlib import Path
from statistics import NormalDist
import unittest

import numpy as np

from checksi.dp5q.scoring import ScoringError, aggregate_compatibility, diagnostic_errors, fit_gaussian

REFERENCE = json.loads((Path(__file__).parent / 'fixtures/dp5q/scoped_numerical_reference.json').read_text())


class ScopedScoringTests(unittest.TestCase):
    def test_shared_anchor_score_rejects_averaging_and_independent_duplication_mutants(self):
        case = REFERENCE['shared_with_anchor']
        observed = np.array(case['observations'])[case['indices']]
        actual = aggregate_compatibility(observed, [case['predictions']], [[1.] * 3], [1.],
                                         evidence_weights=case['weights'])
        self.assertAlmostEqual(actual.score, case['score'], places=14)
        self.assertNotAlmostEqual(actual.score, 1., places=6)
        duplicated = aggregate_compatibility(observed, [case['predictions']], [[1.] * 3], [1.])
        self.assertAlmostEqual(duplicated.score, case['naive_duplication_mutant_score'], places=14)
        self.assertNotAlmostEqual(actual.score, duplicated.score, places=6)

    def test_conformer_mixture_occurs_before_weighted_geometric_aggregation(self):
        actual = aggregate_compatibility([20., 20., 100.], [[19., 21., 100.], [18., 22., 100.]],
            [[1.] * 3] * 2, [.25, .75], evidence_weights=[.5, .5, 1.])
        expected = math.sqrt(.25 * math.erfc(1 / math.sqrt(2)) + .75 * math.erfc(2 / math.sqrt(2)))
        self.assertAlmostEqual(actual.score, expected, places=14)

    def test_shared_environment_quantiles_keep_distinct_fits(self):
        levels = np.arange(1, 100) / 100.
        fits = [fit_gaussian([mean + 2 * NormalDist().inv_cdf(level) for level in levels], levels)
                for mean in (19., 21.)]
        self.assertAlmostEqual(fits[0].mu, 19., places=10)
        self.assertAlmostEqual(fits[1].mu, 21., places=10)
        for fit in fits:
            self.assertAlmostEqual(fit.sigma, 2., places=10)
        actual = aggregate_compatibility([20., 20., 100.], [[fits[0].mu, fits[1].mu, 100.]],
            [[fits[0].sigma, fits[1].sigma, 1.]], [1.], evidence_weights=[.5, .5, 1.])
        self.assertAlmostEqual(actual.score, math.sqrt(math.erfc(.5 / math.sqrt(2))), places=10)
        self.assertNotAlmostEqual(actual.score, 1., places=6)

    def test_weighted_fit_matches_independent_anchored_reference_without_changing_score_inputs(self):
        case = REFERENCE['anchored_weighted_fit']
        predictions = np.array([case['y_predicted']])
        original = predictions.copy()
        result = diagnostic_errors(predictions, case['x_observed'], [1.], evidence_weights=case['weights'])
        np.testing.assert_allclose(result['scaled_errors'], case['scaled_residuals'], atol=1e-12, rtol=0)
        self.assertAlmostEqual(result['corrected_mae'], case['weighted_mae'], places=12)
        self.assertAlmostEqual(result['raw_mae'], 5 / 3, places=12)
        np.testing.assert_array_equal(predictions, original)

    def test_unit_evidence_weights_preserve_complete_spectrum_numerics(self):
        args = ([20., 50., 100.], [[19., 49., 99.]], [[1., 2., 3.]], [1.])
        before = aggregate_compatibility(*args)
        after = aggregate_compatibility(*args, evidence_weights=[1., 1., 1.])
        self.assertEqual(after.score, before.score)
        self.assertEqual(after.log_score, before.log_score)
        self.assertEqual(diagnostic_errors(args[1], args[0], args[3]),
                         diagnostic_errors(args[1], args[0], args[3], evidence_weights=[1., 1., 1.]))

    def test_shared_sole_observation_cannot_supply_a_regression(self):
        score = aggregate_compatibility([20., 20.], [[19., 21.]], [[1., 1.]], [1.],
                                        evidence_weights=[.5, .5])
        self.assertAlmostEqual(score.score, REFERENCE['shared_identity']['score'], places=14)
        result = diagnostic_errors([[19., 21.]], [20., 20.], [1.], evidence_weights=[.5, .5])
        self.assertEqual(result, {'raw_mae': 1., 'corrected_mae': None, 'scaled_errors': None,
                                  'regression_reason': 'insufficient_signals'})

    def test_splitting_measurement_weight_preserves_near_threshold_fit_availability(self):
        ordinary = diagnostic_errors([[101., 101.000025]], [100., 100.000025], [1.])
        shared = diagnostic_errors([[101., 101., 101.000025]],
                                   [100., 100., 100.000025], [1.], evidence_weights=[.5, .5, 1.])
        self.assertIsNone(ordinary['regression_reason'])
        self.assertIsNone(shared['regression_reason'])
        self.assertAlmostEqual(shared['corrected_mae'], ordinary['corrected_mae'], places=10)
        self.assertEqual(shared['raw_mae'], ordinary['raw_mae'])

    def test_invalid_or_omitted_zero_weights_cannot_enter_scored_terms(self):
        for weights in ([0., 1.], [-.5, 1.], [2., 1.], [float('nan'), 1.], [.5]):
            with self.subTest(weights=weights):
                with self.assertRaises(ScoringError):
                    aggregate_compatibility([20., 50.], [[19., 49.]], [[1., 1.]], [1.], evidence_weights=weights)
                with self.assertRaises(ScoringError):
                    diagnostic_errors([[19., 49.]], [20., 50.], [1.], evidence_weights=weights)


if __name__ == '__main__':
    unittest.main()
