"""The host binds complete worker results without repeating chemical calculations."""

import unittest
from unittest.mock import patch

from checksi.config import ConfigManager
from checksi.dp5_enrichment import enrich_analysis_with_dp5
from checksi.dp5q.contracts import CheckRequest, validate_result_for_request
from checksi.dp5q.engine import check_compound
from checksi.dp5q.network import load_models
from checksi.dp5q.scoring import aggregate_compatibility


class ResultBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        models = load_models()
        cls.request = CheckRequest(compound_id='binding', smiles='CCO', carbon_shifts=[20., 60.])
        cls.result = check_compound(cls.request, models)
        metadata = models.metadata
        cls.ready = {'capabilities': ['coincident-observations-v1'], 'model_id': metadata['model_id'],
                     'preprocessing_id': metadata['preprocessing_id'], 'score_version': metadata['score_id'],
                     'geometry_policy': metadata['geometry_policy'],
                     'weights_sha256': metadata['files']['weights.npz']['sha256']}

    def test_host_does_not_repeat_partition_enumeration_or_score_aggregation(self):
        self.assertEqual(self.result.status, 'assessed', self.result)
        with patch('checksi.analysis.structure_analysis.analyze_molecular_structure_detailed',
                   side_effect=AssertionError('host repeated source analysis')), patch(
                'checksi.analysis.structure_analysis.enumerate_supported_stereoisomers',
                side_effect=AssertionError('host repeated stereo enumeration')), patch(
                'checksi.dp5q.scoring.aggregate_compatibility',
                side_effect=AssertionError('host repeated score aggregation')):
            validate_result_for_request(self.request, self.result, self.ready)

    def test_host_still_rejects_changes_to_the_indexed_observation(self):
        rows = [dict(row) for row in self.result.assignments]
        rows[0]['experimental_shift'] += 1.
        changed = self.result.model_copy(update={'assignments': rows})
        with self.assertRaisesRegex(ValueError, 'experimental value'):
            validate_result_for_request(self.request, changed, self.ready)

    def test_engine_owns_the_consistency_of_fitted_distributions_and_compatibility(self):
        result = self.result
        diagnostics = result.diagnostics
        independently_aggregated = aggregate_compatibility(
            [row['experimental_shift'] for row in result.assignments],
            diagnostics['fit_locations'], diagnostics['fit_sigmas'], diagnostics['populations'],
            evidence_weights=[row['evidence_weight'] for row in result.assignments])
        self.assertAlmostEqual(result.score_raw, independently_aggregated.score, places=12)
        self.assertAlmostEqual(result.log_score, independently_aggregated.log_score, places=12)

    def test_real_supervised_component_score_keeps_source_labels_and_exact_warning(self):
        config = ConfigManager.load_factory()
        config.dp5.enabled = True
        compounds = [{'id': index, 'smiles': smiles, 'run_dp5': True,
                      'dp5_c_signals_list': [20., 180.], 'dp5_carbon_groups': []}
                     for index, smiles in enumerate(('[Na+].CC(=O)[O-]', 'O.CC(=O)O'), 1)]
        enriched = enrich_analysis_with_dp5(config, {'compounds': compounds})
        self.assertEqual(enriched['dp5_summary']['successful'], 2, compounds)
        for compound in compounds:
            self.assertEqual(sorted(label for row in compound['dp5']['atoms'] for label in row['atom_labels']),
                             ['C2', 'C3'])
            self.assertIn('DP5q calculated for the organic component only; the counter-ion/solvate was ignored, '
                          'and protonation or salt formation may shift some 13C signals.',
                          ' '.join(compound['all_warnings']))


if __name__ == '__main__':
    unittest.main()
