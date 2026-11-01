"""Overlap annotations describe formal environments rather than atom counts."""

from dataclasses import asdict
import unittest

from checksi.dp5_enrichment import _compound_request
from checksi.dp5q.observation_assignment import assign_observations
from tests.test_carbon_observations import carbon, config


DIARYL = 'C[C@@H](O)C(c1ccccc1)c1ccccc1'
DIARYL_SHIFTS = '142.0 (2C), 128.6 (2C), 128.3 (2C), 126.7 (2C), 69.9, 60.5, 21.6'
# Original SMILES atom labels, independently partitioned by aryl position:
# the two rings are diastereotopic; each ortho/meta position contains two atoms.
FORMAL = [('C5',), ('C11',), ('C6', 'C10'), ('C12', 'C16'),
          ('C7', 'C9'), ('C13', 'C15'), ('C8',), ('C14',), ('C1',), ('C2',), ('C4',)]
# Engineering observation assignments, NOT true-equivalence groups for prediction averaging.
REPORTED_ATOM_SETS = [('C5', 'C11'), ('C6', 'C10', 'C12', 'C16'),
            ('C7', 'C9', 'C13', 'C15'), ('C8', 'C14'), ('C1',), ('C2',), ('C4',)]


def independent_cardinalities(groups, formal):
    """Count complete source environments, never individual atoms."""
    return [sum(set(environment) <= set(group) for environment in formal) for group in groups]


class CarbonEnvironmentConstraintTests(unittest.TestCase):
    def test_multi_atom_overlap_preserves_full_partitions_and_original_observations(self):
        expected = independent_cardinalities(REPORTED_ATOM_SETS, FORMAL)
        self.assertEqual(expected, [2, 2, 2, 2, 1, 1, 1])
        for disabled in (False, True):
            observations, details = [], {}
            checked, tracker, structure = carbon(DIARYL, 'C15H16O', DIARYL_SHIFTS,
                disabled=disabled, observations_out=observations, dp5_refusal_details=details)
            self.assertEqual(structure.carbon_count, 11)
            self.assertEqual(checked[5], structure.carbon_groups)
            self.assertEqual({frozenset(group) for group in structure.carbon_groups},
                             {frozenset(group) for group in FORMAL if len(group) > 1})
            self.assertTrue(checked[6], details)
            self.assertEqual(details, {})
            self.assertEqual(tracker.get_count('c13_count_structure_check'), 0)
            self.assertEqual([item.shift for item in observations], checked[7])
            request = _compound_request(config(), {'smiles': DIARYL, 'run_dp5': checked[6],
                'dp5_carbon_groups': checked[5], 'dp5_c_signals_list': checked[7],
                'carbon_observations': [asdict(item) for item in observations]}, 1)
            self.assertEqual(request.observation_atom_counts, (2, 2, 2, 2, None, None, None))
            self.assertEqual(len(request.carbon_shifts), 7)

    def test_symmetry_atom_size_cannot_validate_a_wrong_peak_overlap(self):
        for disabled in (False, True):
            details = {}
            checked, _, _ = carbon('Cc1ccc(F)cc1', 'C7H7F',
                '161.0, 133.5 (2C), 130.5, 114.9, 20.8', disabled=disabled, dp5_refusal_details=details)
            self.assertFalse(checked[6])
            self.assertEqual(details['cause'], 'annotation_partition_mismatch')
            self.assertEqual(len(checked[7]), 5)


    def test_full_environments_share_only_the_declared_observation_capacities(self):
        calculated = [142., 142., 128.6, 128.6, 128.3, 128.3, 126.7, 126.7, 69.9, 60.5, 21.6]
        observations = [142., 128.6, 128.3, 126.7, 69.9, 60.5, 21.6]
        permutation = [6, 4, 2, 0, 5, 3, 1]
        observed = [observations[index] for index in permutation]
        counts = [2 if index < 4 else None for index in permutation]
        assignment = assign_observations(calculated, observed, observation_atom_counts=counts)
        self.assertEqual([observed[index] for index in assignment.indices], calculated)
        self.assertEqual(assignment.evidence_weights.tolist(), [.5] * 8 + [1.] * 3)

    def test_engine_refuses_symmetry_annotations_before_geometry(self):
        from checksi.dp5q.contracts import CheckRequest
        from checksi.dp5q.engine import check_compound
        from checksi.dp5q.network import load_models
        stages = []
        request = CheckRequest(compound_id='acetone', smiles='CC(=O)C', carbon_shifts=[207.1, 30.9],
            carbon_groups=[['C1', 'C4']], observation_atom_counts=[None, 2])
        result = check_compound(request, load_models(), progress=stages.append)
        self.assertEqual((result.status, result.reason), ('not_applicable', 'annotation_partition_mismatch'))
        self.assertNotIn('geometry', stages)


class CarbonEnvironmentEngineTests(unittest.TestCase):
    def test_real_diaryl_assignment_and_host_validation_share_signal_units(self):
        from checksi.dp5q.contracts import validate_result_for_request
        from checksi.dp5q.engine import check_compound
        from checksi.dp5q.network import load_models

        observations = []
        checked, _, _ = carbon(DIARYL, 'C15H16O', DIARYL_SHIFTS, observations_out=observations)
        request = _compound_request(config(), {'smiles': DIARYL, 'run_dp5': checked[6],
            'dp5_carbon_groups': checked[5], 'dp5_c_signals_list': checked[7],
            'carbon_observations': [asdict(item) for item in observations]}, 1)
        # The aromatic peaks alone have equal-cost alternatives. An independent
        # group/peak mapping isolates cardinality from that existing refusal.
        full = [tuple(group) for group in request.carbon_groups]
        used = {label for group in full for label in group}
        full.extend((label,) for label in request.carbon_labels if label not in used)
        fixed = {index: next(peak for peak, members in enumerate(REPORTED_ATOM_SETS)
                             if set(group) <= set(members)) for index, group in enumerate(full)}
        self.assertEqual({frozenset(group) for group in full}, {frozenset(group) for group in FORMAL})
        request = request.model_copy(update={'fixed_assignments': fixed})
        models = load_models()
        result = check_compound(request, models)
        self.assertEqual(result.status, 'assessed', result)
        ready = {'capabilities': ['coincident-observations-v1'], 'model_id': models.metadata['model_id'], 'preprocessing_id': models.metadata['preprocessing_id'],
                 'score_version': models.metadata['score_id'], 'weights_sha256': models.metadata['files']['weights.npz']['sha256'],
                 'geometry_policy': 'converged-single-v2'}
        validate_result_for_request(request, result, ready)
        self.assertEqual(len(result.assignments), 11)
        for row in result.assignments:
            contributors = sum(value == row['observation_index'] for value in fixed.values())
            self.assertEqual(row['evidence_weight'], 1/contributors)
            annotated = request.observation_atom_counts[row['observation_index']]
            if annotated is not None:
                self.assertEqual(annotated, contributors)
        self.assertEqual(sum(row['evidence_weight'] for row in result.assignments), 7.)
        rows = [dict(row) for row in result.assignments]
        rows[0]['observation_index'], rows[4]['observation_index'] = rows[4]['observation_index'], rows[0]['observation_index']
        for row in rows:
            row['experimental_shift'] = request.carbon_shifts[row['observation_index']]
        with self.assertRaisesRegex(ValueError, 'weights'):
            validate_result_for_request(request.model_copy(update={'fixed_assignments': {}}),
                                        result.model_copy(update={'assignments': rows}), ready)
