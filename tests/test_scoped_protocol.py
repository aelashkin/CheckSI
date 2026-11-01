"""Actual bundled-worker/host controls for the protocol2 observation contract."""

from collections import Counter
from copy import deepcopy
import math
import unittest

from checksi.dp5q.client import DP5qSession
from checksi.dp5q.contracts import CheckRequest, CheckResult, Frame, decode_frame, encode_frame, validate_result_for_request


NATIVE_CASES = (
    ('valine', 'CC(C)[C@H](N)C(=O)O', (), ('C1', 'C2', 'C3', 'C4', 'C6'),
     (175., 60., 32., 20.), ((0, 2),), {0: 3, 1: 2, 2: 3, 3: 1, 4: 0}),
    ('native04-tartrate', 'CC(C)OC(=O)[C@H](O)[C@@H](O)C(=O)OC(C)C',
     (('C1', 'C15'), ('C2', 'C14'), ('C3', 'C16'), ('C5', 'C11'), ('C7', 'C9')),
     ('C1', 'C2', 'C3', 'C5', 'C7', 'C9', 'C11', 'C14', 'C15', 'C16'),
     (170., 72., 69., 20.), ((0, 2),), {0: 3, 1: 2, 2: 3, 3: 0, 4: 1}),
    ('sulfoxide', 'C[S@](=O)C(C)C', (), ('C1', 'C4', 'C5', 'C6'),
     (40., 52., 20.), ((2, 3),), {0: 0, 1: 1, 2: 2, 3: 2}),
)


def request_for(case):
    name, smiles, groups, labels, shifts, coalition, fixed = case
    return CheckRequest(compound_id=name, smiles=smiles, carbon_groups=groups, carbon_labels=labels,
                        carbon_shifts=shifts, coincident_environments=coalition,
                        fixed_assignments=fixed, include_geometry=True)


def coherent_single_conformer_claim(values):
    """Recompute an adversary's arithmetic from its altered rows and weights."""
    rows, diagnostics = values['assignments'], values['diagnostics']
    weights = [row['evidence_weight'] for row in rows]
    denominator = sum(weights)
    x = [row['experimental_shift'] for row in rows]
    y = [row['calculated_shift'] for row in rows]
    probabilities = [math.erfc(abs(value - mu) / sigma / math.sqrt(2)) for value, mu, sigma in
                     zip(x, diagnostics['fit_locations'][0], diagnostics['fit_sigmas'][0])]
    if not all(value > 0 for value in probabilities):
        raise AssertionError('the coherent control requires representable per-row tails')
    for row, probability in zip(rows, probabilities):
        row['score_percent'] = row['atom_probability_percent'] = 100 * probability
    values['log_score'] = sum(weight * math.log(value) for weight, value in zip(weights, probabilities)) / denominator
    values['score_raw'] = math.exp(values['log_score'])
    diagnostics.pop('zero_score_reason', None)
    objective = sum(weight * abs(predicted - observed) for weight, predicted, observed in zip(weights, y, x))
    diagnostics.update(assignment_objective=objective, raw_mae=objective / denominator)
    x_mean = sum(weight * value for weight, value in zip(weights, x)) / denominator
    y_mean = sum(weight * value for weight, value in zip(weights, y)) / denominator
    slope = sum(weight * (observed - x_mean) * (predicted - y_mean) for weight, observed, predicted in
                zip(weights, x, y)) / sum(weight * (value - x_mean) ** 2 for weight, value in zip(weights, x))
    intercept = y_mean - slope * x_mean
    scaled = [(predicted - intercept) / slope - observed for observed, predicted in zip(x, y)]
    diagnostics.update(regression_reason=None, scaled_errors=scaled,
                       corrected_mae=sum(weight * abs(value) for weight, value in zip(weights, scaled)) / denominator)
    for row, error in zip(rows, scaled):
        row['scaled_error'] = error
    contributors = {}
    for index, row in enumerate(rows):
        contributors.setdefault(str(row['observation_index']), []).append(index)
    diagnostics['coincident_observations'] = {key: indices for key, indices in contributors.items() if len(indices) > 1}
    diagnostics['score_scope'] = 'coincident_observations' if diagnostics['coincident_observations'] else 'complete_spectrum'
    return values


class ScopedWireContractTests(unittest.TestCase):
    def test_canonical_fixed_json_roundtrip_preserves_shared_original_indices(self):
        request = request_for(NATIVE_CASES[0])
        raw = encode_frame(Frame(kind='check', run_id='scoped', generation=1, sequence=1,
                                 payload=request.model_dump(mode='json')))
        frame = decode_frame(raw)
        self.assertEqual(frame.version, 2)
        rebuilt = CheckRequest.model_validate(frame.payload)
        self.assertEqual(rebuilt.fixed_assignments, request.fixed_assignments)
        self.assertEqual(rebuilt.coincident_environments, ((0, 2),))
        self.assertTrue(all(isinstance(key, str) for key in frame.payload['fixed_assignments']))

    def test_legacy_version_and_public_omission_field_are_not_supported(self):
        with self.assertRaises(ValueError):
            Frame(version=1, kind='check', run_id='old', generation=1, sequence=1)
        values = {'compound_id': 'no-omission', 'smiles': 'CCO', 'carbon_shifts': [10., 20.]}
        with self.assertRaises(ValueError):
            CheckRequest(**values, omittable_environments=[])

    def test_coalition_and_fixed_values_are_strict_bounded_nonboolean_indices(self):
        base = {'compound_id': 'v', 'smiles': NATIVE_CASES[0][1], 'carbon_shifts': [175., 60., 32., 20.],
                'carbon_labels': NATIVE_CASES[0][3]}
        for coalitions in (((0, True),), (('0', 2),), ((0, 0),), ((0,),), ((0, 2), (2, 3)), ((0, 5),)):
            with self.subTest(coalitions=coalitions), self.assertRaises(ValueError):
                CheckRequest(**base, coincident_environments=coalitions)
        for fixed in ({'01': 3}, {True: 3}, {'0': '3'}, {0: 3, '0': 2}):
            with self.subTest(fixed=fixed), self.assertRaises(ValueError):
                CheckRequest(**base, coincident_environments=((0, 2),), fixed_assignments=fixed)


class ActualScopedWorkerTests(unittest.TestCase):
    def test_coalition_member_order_does_not_change_its_set_meaning(self):
        request = request_for(NATIVE_CASES[0]).model_copy(update={'coincident_environments': ((2, 0),)})
        with DP5qSession() as session:
            result = session.check(request)
            self.assertEqual(result.status, 'assessed', result)
            self.assertEqual(result.diagnostics['coincident_observations'], {'3': [0, 2]})

    def test_native_named_cases_and_a_b_a_share_one_actual_generation(self):
        with DP5qSession() as session:
            results = []
            for case in (*NATIVE_CASES, NATIVE_CASES[0]):
                request = request_for(case)
                result = session.check(request)
                self.assertEqual(result.status, 'assessed', result)
                self.assertEqual(len(result.assignments), len(case[-1]))
                counts = Counter(case[-1].values())
                for index, row in enumerate(result.assignments):
                    self.assertEqual(row['observation_index'], case[-1][index])
                    self.assertEqual(row['experimental_shift'], case[4][case[-1][index]])
                    self.assertEqual(row['evidence_weight'], 1 / counts[case[-1][index]])
                self.assertEqual(sum(row['evidence_weight'] for row in result.assignments), len(case[4]))
                self.assertEqual(result.diagnostics['full_environment_count'], len(case[-1]))
                self.assertEqual(result.diagnostics['observation_count'], len(case[4]))
                self.assertEqual(result.diagnostics['scored_environment_count'], len(case[-1]))
                self.assertEqual(result.diagnostics['score_scope'], 'coincident_observations')
                self.assertEqual(result.provenance['coordinate_partition_verified'],
                                 case[0] != 'native04-tartrate')
                if case[0] == 'native04-tartrate':
                    self.assertEqual(result.provenance['coordinate_partition_reason'], 'coordinate_partition_mismatch')
                self.assertTrue(result.provenance['selected_isomer_smiles'])
                for first, second in case[5]:
                    self.assertNotEqual(result.assignments[first]['calculated_shift'],
                                        result.assignments[second]['calculated_shift'])
                    self.assertNotEqual(
                        (result.diagnostics['fit_locations'][0][first], result.diagnostics['fit_sigmas'][0][first]),
                        (result.diagnostics['fit_locations'][0][second], result.diagnostics['fit_sigmas'][0][second]))
                self.assertEqual(session.metadata['capabilities'], ['coincident-observations-v1'])
                validate_result_for_request(request, result, session.metadata)
                results.append(result)
            self.assertEqual(results[0].score_raw, results[-1].score_raw)
            self.assertEqual(results[0].assignments, results[-1].assignments)
            self.assertEqual(len(session._processes), 1)

    def test_sole_shared_original_observation_has_no_fitted_correction(self):
        request = CheckRequest(compound_id='sole-original-observation', smiles='CCO',
            carbon_labels=('C1', 'C2'), carbon_shifts=(30.,), observation_atom_counts=(2,),
            fixed_assignments={0: 0, 1: 0})
        with DP5qSession() as session:
            result = session.check(request)
            self.assertEqual(result.status, 'assessed', result)
            self.assertEqual([row['observation_index'] for row in result.assignments], [0, 0])
            self.assertEqual([row['evidence_weight'] for row in result.assignments], [.5, .5])
            self.assertEqual(result.diagnostics['observation_count'], 1)
            self.assertEqual(result.diagnostics['regression_reason'], 'insufficient_signals')
            self.assertIsNone(result.diagnostics['corrected_mae'])
            self.assertIsNone(result.diagnostics['scaled_errors'])
            validate_result_for_request(request, result, session.metadata)

    def test_coherent_reweighted_and_omitted_terminals_are_rejected(self):
        request = request_for(NATIVE_CASES[0])
        with DP5qSession() as session:
            actual = session.check(request)
            self.assertEqual(actual.status, 'assessed', actual)
            for change in ('duplicate-evidence', 'omission', 'dropped-environment', 'unjustified-coalition',
                           'wrong-partition-claim', 'wrong-source-stereo', 'invalid-fit-width'):
                values = deepcopy(actual.model_dump(mode='python'))
                validating = request
                if change == 'duplicate-evidence':
                    for row in values['assignments']:
                        row['evidence_weight'] = 1.
                    coherent_single_conformer_claim(values)
                elif change == 'omission':
                    values['assignments'][0].update(observation_index=None, experimental_shift=None, evidence_weight=0.)
                elif change == 'dropped-environment':
                    values['assignments'].pop(0)
                    for key in ('fit_locations', 'fit_sigmas', 'fit_residual_rms', 'quantile_crossings'):
                        for row in values['diagnostics'][key]:
                            row.pop(0)
                    for row in values['assignments']:
                        row['evidence_weight'] = 1.
                    values['diagnostics'].update(full_environment_count=4, scored_environment_count=4)
                    coherent_single_conformer_claim(values)
                elif change == 'unjustified-coalition':
                    rows = values['assignments']
                    rows[0]['observation_index'], rows[1]['observation_index'] = 2, 3
                    rows[0]['experimental_shift'], rows[1]['experimental_shift'] = 32., 20.
                    rows[0]['evidence_weight'], rows[1]['evidence_weight'] = 1., .5
                    coherent_single_conformer_claim(values)
                    validating = request.model_copy(update={'fixed_assignments': {}})
                elif change == 'wrong-partition-claim':
                    values['provenance']['partition_constraint'] = 'count-only'
                elif change == 'wrong-source-stereo':
                    values['provenance']['selected_isomer_smiles'] = 'CC(C)[C@@H](N)C(=O)O'
                else:
                    values['diagnostics']['fit_sigmas'][0][0] = 0.
                with self.subTest(change=change), self.assertRaises(ValueError):
                    validate_result_for_request(validating, CheckResult.model_validate(values), session.metadata)


if __name__ == '__main__':
    unittest.main()
