"""Independent controls for complete spectra with shared observations."""

import unittest
from itertools import product
from random import Random

import numpy as np

from checksi.dp5q.assignment import AssignmentError
from checksi.dp5q.observation_assignment import assign_observations, validate_observation_coverage
from tests.dp5q_observation_reference import exhaustive_reference, mapping_weights


class ScopedAssignmentTests(unittest.TestCase):
    def test_seeded_capacity_coalition_and_fixed_cases_match_exhaustive_minima(self):
        random = Random(45002)
        for case in range(500):
            count = random.randint(2, 4)
            observed_count = random.randint(1, count)
            predicted = [float(random.randrange(-3, 4)) for _ in range(count)]
            observed = [float(random.randrange(-3, 4)) for _ in range(observed_count)]
            capacities = [1] * observed_count
            for _ in range(count - observed_count):
                capacities[random.randrange(observed_count)] += 1
            capacities = [value if random.randrange(2) else None for value in capacities]
            sharing = [(0, 1)] if case % 3 else []
            fixed = {random.randrange(count): random.randrange(observed_count)} if case % 5 else {}
            with self.subTest(case=case):
                try:
                    expected = exhaustive_reference(predicted, observed, capacities=capacities,
                                                    sharing=sharing, fixed=fixed)
                except ValueError:
                    with self.assertRaises(AssignmentError):
                        assign_observations(predicted, observed, observation_atom_counts=capacities,
                                            coincident_environments=sharing, fixed=fixed)
                    continue
                actual = assign_observations(predicted, observed, observation_atom_counts=capacities,
                                             coincident_environments=sharing, fixed=fixed)
                mapping = tuple(int(index) for index in actual.indices)
                weights = mapping_weights(mapping, observed, capacities, {frozenset(group) for group in sharing}, fixed)
                self.assertEqual(actual.evidence_weights.tolist(), weights)
                self.assertAlmostEqual(sum(weight * (predicted[index] - observed[peak]) ** 2
                                          for index, (weight, peak) in enumerate(zip(weights, mapping))),
                                       expected['objective'])

    def test_coverage_preflight_uses_counts_and_relations_without_predictions(self):
        validate_observation_coverage(3, 2, coincident_environments=[(0, 1)])
        validate_observation_coverage(3, 2, observation_atom_counts=[2, 1], fixed={0: 0, 1: 0})
        with self.assertRaises(AssignmentError):
            validate_observation_coverage(3, 2)
        with self.assertRaises(AssignmentError):
            validate_observation_coverage(2, 2, observation_atom_counts=[2, 1])

    def test_tiny_policies_match_the_reviewed_independent_exhaustive_oracle(self):
        cases = []
        for first, second in product((18., 20., 22.), repeat=2):
            cases.append(([first, second, 100.], [20., 100.], {'capacities': [2, 1]}))
            cases.append(([first, second, 100.], [20., 100.], {'sharing': [(0, 1)]}))
        cases.extend([
            ([19., 21., 19.], [20., 20.], {'capacities': [1, 2]}),
            ([19., 21., 100.], [20., 100.], {'sharing': [(0, 1)], 'fixed': {0: 0, 1: 0}}),
        ])
        rename = {'capacities': 'observation_atom_counts', 'sharing': 'coincident_environments',
                  'fixed': 'fixed'}
        for predicted, observed, options in cases:
            with self.subTest(predicted=predicted, observed=observed, options=options):
                kwargs = {rename[key]: value for key, value in options.items()}
                try:
                    expected = exhaustive_reference(predicted, observed, **options)
                except ValueError:
                    with self.assertRaises(AssignmentError):
                        assign_observations(predicted, observed, **kwargs)
                    continue
                actual = assign_observations(predicted, observed, **kwargs)
                if not expected['tied']:
                    self.assertEqual(actual.indices.tolist(), list(expected['indices']))
                    self.assertEqual(actual.evidence_weights.tolist(), expected['weights'])
                else:
                    weights = mapping_weights(tuple(int(i) for i in actual.indices), observed,
                        options.get('capacities', [None] * len(observed)),
                        {frozenset(group) for group in options.get('sharing', ())},
                        options.get('fixed', {}))
                    self.assertEqual(actual.evidence_weights.tolist(), weights)
                squared_cost = sum(weight * (predicted[group] - observed[peak]) ** 2
                    for group, (weight, peak) in enumerate(zip(actual.evidence_weights, actual.indices)) if peak >= 0)
                self.assertAlmostEqual(squared_cost, expected['objective'], places=12)
                self.assertAlmostEqual(actual.objective, sum(weight * abs(predicted[group] - observed[peak])
                    for group, (weight, peak) in enumerate(zip(actual.evidence_weights, actual.indices)) if peak >= 0))

    def test_explicit_capacity_preserves_both_methyl_residuals(self):
        result = assign_observations([19., 21., 100.], [20., 100.], observation_atom_counts=[2, None])
        self.assertEqual(result.indices.tolist(), [0, 0, 1])
        self.assertEqual(result.evidence_weights.tolist(), [.5, .5, 1.])
        self.assertEqual((np.array([19., 21., 100.]) - np.array([20., 100.])[result.indices]).tolist(), [-1., 1., 0.])
        self.assertEqual(result.objective, 1.)
        self.assertEqual(result.coincident_observations, {0: (0, 1)})

    def test_structural_coalition_shares_only_its_specific_environments(self):
        result = assign_observations([19., 21., 100.], [20., 100.], coincident_environments=[(0, 1)])
        self.assertEqual(result.indices.tolist(), [0, 0, 1])
        self.assertEqual(result.evidence_weights.tolist(), [.5, .5, 1.])

    def test_missing_peak_without_authorized_sharing_is_refused(self):
        with self.assertRaises(AssignmentError):
            assign_observations([19., 21., 100.], [20., 100.])

    def test_shared_fixed_assignments_use_original_observation_indices(self):
        result = assign_observations([19., 21., 100.], [20., 100.],
            observation_atom_counts=[2, 1], fixed={0: 0, 1: 0, 2: 1})
        self.assertEqual(result.indices.tolist(), [0, 0, 1])
        for fixed in ({True: 0}, {0: True}, {'0': 0}, {0: '0'}, {0: 2}, {3: 0}):
            with self.subTest(fixed=fixed), self.assertRaises(AssignmentError):
                assign_observations([19., 21., 100.], [20., 100.],
                    observation_atom_counts=[2, 1], fixed=fixed)

    def test_fixed_overcapacity_and_conflicting_coalition_are_refused(self):
        with self.assertRaises(AssignmentError):
            assign_observations([19., 21., 100.], [20., 100.],
                observation_atom_counts=[2, 1], fixed={0: 1, 1: 1})
        with self.assertRaises(AssignmentError):
            assign_observations([19., 21., 100.], [20., 100.],
                coincident_environments=[(0, 1)], fixed={0: 0, 1: 1})

    def test_fixed_coalition_can_select_a_more_distant_original_peak(self):
        result = assign_observations([19., 21., 100.], [20., 100.],
            coincident_environments=[(0, 1)], fixed={0: 1, 1: 1})
        self.assertEqual(result.indices.tolist(), [1, 1, 0])

    def test_equal_ppm_different_capacity_weight_tie_keeps_first_solution(self):
        result = assign_observations([19., 21., 19.], [20., 20.], observation_atom_counts=[1, 2])
        self.assertEqual(result.indices.tolist(), [1, 1, 0])
        self.assertEqual(result.evidence_weights.tolist(), [.5, .5, 1.])
        self.assertEqual(result.objective, 2.)

    def test_equal_ppm_equal_unit_weights_preserve_complete_spectrum_behavior(self):
        result = assign_observations([19., 21.], [20., 20.])
        self.assertEqual(result.indices.tolist(), [0, 1])
        self.assertEqual(result.evidence_weights.tolist(), [1., 1.])
        self.assertEqual(result.objective, 2.)



    def test_equal_cost_coalitions_keep_first_policy(self):
        result = assign_observations([0., 0., 0., 0.], [0., 0., 0.],
                                     coincident_environments=[(0, 1), (2, 3)])
        self.assertEqual(result.indices.tolist(), [0, 0, 1, 2])
        self.assertEqual(result.evidence_weights.tolist(), [.5, .5, 1., 1.])
        self.assertEqual(result.coincident_observations, {0: (0, 1)})
        self.assertEqual(result.objective, 0.)


    def test_shared_observations_minimize_weighted_squared_residuals(self):
        result = assign_observations([0., 10., 30.], [20., 40.], observation_atom_counts=[2, 1])
        self.assertEqual(result.indices.tolist(), [0, 0, 1])
        self.assertEqual(result.objective, .5 * 20 + .5 * 10 + 10)

    def test_extreme_cost_scale_is_common_to_every_candidate_policy(self):
        predicted, observed = np.array([0., 10., 30., 100.]), np.array([20., 40., 100.])
        options = {'coincident_environments': [(0, 1), (2, 3)]}
        normal = assign_observations(predicted, observed, **options)
        extreme = assign_observations(predicted * 1e200, observed * 1e200, **options)
        self.assertEqual(extreme.indices.tolist(), normal.indices.tolist())
        self.assertEqual(extreme.evidence_weights.tolist(), normal.evidence_weights.tolist())
        self.assertAlmostEqual(extreme.objective / 1e200, normal.objective)


    def test_malformed_capacities_and_coalitions_fail_closed(self):
        cases = [dict(observation_atom_counts=[True, 2]), dict(observation_atom_counts=[0, 3]),
            dict(observation_atom_counts=['2', 1]), dict(coincident_environments=[(0, 0)]),
            dict(coincident_environments=[(0, 1), (1, 2)]), dict(coincident_environments=[(0, True)]),
            dict(coincident_environments=[(0, 3)])]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(AssignmentError):
                assign_observations([19., 21., 100.], [20., 100.], **kwargs)

    def test_complete_policy_budget_is_bounded_before_returning_an_optimum(self):
        with self.assertRaises(AssignmentError) as raised:
            assign_observations(list(range(17)), list(range(16)),
                coincident_environments=[(i, i + 1) for i in range(0, 16, 2)])
        self.assertEqual(raised.exception.reason, 'assignment_search_limit')

    def test_fixed_map_prunes_contradictory_policies_before_the_policy_budget(self):
        predicted = [0., 0.] + [float(value) for value in range(1, 16)]
        observed = [float(value) for value in range(16)]
        fixed = {0: 0, 1: 0, **{group: group - 1 for group in range(2, 17)}}
        result = assign_observations(predicted, observed,
            coincident_environments=[(i, i + 1) for i in range(0, 16, 2)], fixed=fixed)
        self.assertEqual(result.indices.tolist(), [fixed[i] for i in range(17)])
        self.assertEqual(result.evidence_weights.tolist(), [.5, .5] + [1.] * 15)
        self.assertEqual(result.objective, 0.)


    def test_fully_fixed_large_policies_prune_generation_itself(self):
        cases = (
            ([float(i // 2) for i in range(128)], list(range(64)),
             {'coincident_environments': [(i, i + 1) for i in range(0, 128, 2)],
              'fixed': {i: i // 2 for i in range(128)}}, [i // 2 for i in range(128)]),
        )
        for predicted, observed, options, expected in cases:
            with self.subTest(options=tuple(options)):
                visits = 0
                def bounded_cancel():
                    nonlocal visits
                    visits += 1
                    if visits > 1000:
                        raise RuntimeError('unbounded generation control')
                result = assign_observations(predicted, observed, cancel_check=bounded_cancel, **options)
                self.assertEqual(result.indices.tolist(), expected)
                self.assertEqual(result.objective, 0.)
                self.assertLess(visits, 1000)

    def test_cancel_is_observed_during_policy_search(self):
        def cancel():
            raise RuntimeError('cancelled-control')
        with self.assertRaisesRegex(RuntimeError, 'cancelled-control'):
            assign_observations([19., 21., 100.], [20., 100.],
                observation_atom_counts=[2, 1], cancel_check=cancel)


if __name__ == '__main__':
    unittest.main()
