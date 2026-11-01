"""Independent engine controls discriminate accidental environmental averaging."""

import json
from pathlib import Path
from statistics import NormalDist
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

from checksi.analysis.structure_analysis import CoordinateCarbonPartition
from checksi.dp5q.contracts import CheckRequest, validate_result_for_request
from checksi.dp5q.engine import check_compound
from checksi.dp5q.network import Prediction, load_models
from checksi.dp5q.worker import _ready_metadata
import checksi.dp5q.engine as engine

REFERENCE = json.loads((Path(__file__).parent / 'fixtures/dp5q/scoped_numerical_reference.json').read_text())


class ScopedEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.real_models = load_models()

    def controlled_models(self):
        levels = self.real_models.metadata['quantile_levels']
        means = np.array([19., 21., 100.], dtype=np.float32)
        quantiles = np.array([[mean + NormalDist().inv_cdf(level) for level in levels]
                              for mean in means], dtype=np.float32)
        return SimpleNamespace(metadata=self.real_models.metadata,
            predict=Mock(return_value=Prediction(np.array([0, 1, 2]), means, quantiles)))

    def request(self):
        return CheckRequest(compound_id='independent-engine-control', smiles='CCCO',
            carbon_labels=('C1', 'C2', 'C3'), carbon_shifts=(20., 100.),
            observation_atom_counts=(2, 1), fixed_assignments={0: 0, 1: 0, 2: 1})

    def assert_independent_reference(self, result):
        self.assertEqual(result.status, 'assessed', result)
        self.assertEqual([row['calculated_shift'] for row in result.assignments], [19., 21., 100.])
        self.assertEqual([row['observation_index'] for row in result.assignments], [0, 0, 1])
        self.assertEqual([row['evidence_weight'] for row in result.assignments], [.5, .5, 1.])
        self.assertAlmostEqual(result.score_raw, REFERENCE['shared_with_anchor']['score'], delta=1e-6)
        self.assertEqual(result.diagnostics['assignment_objective'], 1.)
        self.assertEqual(result.diagnostics['raw_mae'], .5)
        self.assertNotEqual(result.diagnostics['fit_locations'][0][0], result.diagnostics['fit_locations'][0][1])

    def test_engine_keeps_separate_predictions_fits_and_one_measurement_weight(self):
        models = self.controlled_models()
        request = self.request()
        result = check_compound(request, models)
        self.assert_independent_reference(result)
        validate_result_for_request(request, result, _ready_metadata(models))

    def test_targeted_averaging_mutant_is_detected_by_the_independent_control(self):
        original = engine.group_predictions
        def averaging_mutant(*args):
            means, quantiles = original(*args)
            means[:, :2] = means[:, :2].mean(axis=1, keepdims=True)
            quantiles[:, :2] = quantiles[:, :2].mean(axis=1, keepdims=True)
            return means, quantiles
        with patch.object(engine, 'group_predictions', side_effect=averaging_mutant):
            mutated = check_compound(self.request(), self.controlled_models())
        self.assertEqual(mutated.status, 'assessed', mutated)
        with self.assertRaises(AssertionError):
            self.assert_independent_reference(mutated)

    def test_same_count_wrong_coordinate_partition_keeps_score_without_claiming_proof(self):
        models = self.real_models
        request = CheckRequest(compound_id='wrong-identity', smiles='CC(=O)C',
            carbon_groups=(('C1', 'C4'),), carbon_labels=('C1', 'C2', 'C4'), carbon_shifts=(30., 207.))
        wrong = CoordinateCarbonPartition(groups=((0, 1), (3,)))
        with patch.object(engine, 'carbon_partition_from_coordinates', return_value=wrong), \
                patch.object(models, 'predict', wraps=models.predict) as predict:
            result = check_compound(request, models)
        self.assertEqual((result.status, result.reason), ('assessed', None))
        self.assertFalse(result.provenance['coordinate_partition_verified'])
        self.assertEqual(result.provenance['coordinate_partition_reason'], 'coordinate_partition_mismatch')
        predict.assert_called_once()
        validate_result_for_request(request, result, _ready_metadata(models))


if __name__ == '__main__':
    unittest.main()
