"""Squared-error matching with complete coverage and deterministic ties."""

import unittest

import numpy as np

from checksi.dp5q.assignment import AssignmentError, assign_groups, build_groups, group_predictions


class DP5qAssignmentTests(unittest.TestCase):
    def test_explicit_equivalence_groups_precede_implicit_singletons(self):
        groups = build_groups([0, 2, 4, 5], [["C5", "C1"]])
        self.assertEqual([group.labels for group in groups], [("C1", "C5"), ("C3",), ("C6",)])
        mean, quantiles = group_predictions([[10, 20, 30, 40]], np.arange(12).reshape(1, 4, 3), groups)
        np.testing.assert_array_equal(mean, [[20, 20, 40]])
        np.testing.assert_array_equal(quantiles[0, 0], [3, 4, 5])

    def test_complete_unique_assignment_and_fixed_constraints(self):
        result = assign_groups([10, 20, 30], [29, 11, 19])
        np.testing.assert_array_equal(result.indices, [1, 2, 0])
        fixed = assign_groups([10, 20], [10, 20], fixed={0: 1})
        np.testing.assert_array_equal(fixed.indices, [1, 0])

    def test_equal_mean_different_quantiles_keeps_first_solution(self):
        for order in ([0, 1], [1, 0]):
            with self.subTest(order=order):
                result = assign_groups(np.array([0, 0])[order], [0, 10],
                                       distributions=np.array([[0, 1], [0, 10]])[order])
                self.assertEqual(result.indices.tolist(), [0, 1])
                self.assertEqual(result.objective, 10.)

    def test_same_side_predictions_match_observations_in_sorted_order(self):
        # Absolute errors tie at 50; squared errors select the sorted pairing.
        for predicted in ([0, 10], [10, 0]):
            for observed in ([20, 40], [40, 20]):
                with self.subTest(predicted=predicted, observed=observed):
                    result = assign_groups(predicted, observed, distributions=[[0, 1], [0, 10]])
                    assigned = np.asarray(observed)[result.indices]
                    np.testing.assert_array_equal(assigned[np.argsort(predicted)], [20, 40])
                    self.assertEqual(result.objective, 50.)

    def test_repeated_identical_observations_are_interchangeable(self):
        result = assign_groups([0, 0], [10, 10], distributions=[[0, 1], [0, 10]])
        np.testing.assert_array_equal(np.array([10, 10])[result.indices], [10, 10])

    def test_extreme_finite_squared_cost_does_not_overflow_the_optimizer(self):
        result = assign_groups([0., 1e200], [4e200, 2e200])
        self.assertEqual(result.indices.tolist(), [1, 0])
        self.assertAlmostEqual(result.objective / 1e200, 5.)

    def test_provably_identical_predictors_allow_score_insensitive_ties(self):
        result = assign_groups([0, 0], [0, 10], distributions=[[0, 1], [0, 1]])
        self.assertTrue(result.exchangeable)

    def test_incomplete_coverage_bad_groups_and_fixed_duplicates_fail(self):
        for call in (lambda: assign_groups([1, 2], [1]),
                     lambda: build_groups([0, 1], [["C1", "C3"]]),
                     lambda: assign_groups([1, 2], [1, 2], fixed={0: 0, 1: 0})):
            with self.assertRaises(AssignmentError):
                call()


if __name__ == "__main__":
    unittest.main()
