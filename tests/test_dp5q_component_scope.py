"""Single-carbon-component scoring preserves source labels and chemical scope."""

import unittest
from unittest.mock import patch

from rdkit import Chem

from checksi.analysis.structure_analysis import analyze_molecular_structure_detailed
from checksi.dp5_enrichment import DP5PreparationSkip, enrich_analysis_with_dp5, prepare_dp5_requests
from checksi.dp5q.components import select_scoring_component
from checksi.dp5q.contracts import CheckRequest
from checksi.dp5q.engine import check_compound
from checksi.dp5q.network import load_models
from tests.test_dp5_review_fixes import review_config


COMPONENT_WARNING = ('DP5q calculated for the organic component only; the counter-ion/solvate was ignored, '
                     'and protonation or salt formation may shift some 13C signals.')
MULTIPLE_COMPONENTS = 'DP5q not performed: more than one component contains carbon.'


class OrganicComponentScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.models = load_models()

    def request(self, smiles, *, shifts=None):
        molecule = Chem.MolFromSmiles(smiles)
        structure = analyze_molecular_structure_detailed(molecule)
        return CheckRequest(compound_id='component', smiles=smiles,
            carbon_shifts=shifts or [float(20 + 10*i) for i in range(structure.carbon_count)],
            carbon_groups=structure.carbon_groups,
            carbon_labels=[f'C{atom.GetIdx()+1}' for atom in molecule.GetAtoms() if atom.GetAtomicNum() == 6])

    def test_hcl_hbr_hydrate_and_alkali_counterions_score_the_charged_component(self):
        for source, isolated in (
            ('CC[NH3+].[Cl-]', 'CC[NH3+]'), ('[Br-].CC[NH3+]', 'CC[NH3+]'),
            ('O.CC(=O)O', 'CC(=O)O'), ('[Na+].CC(=O)[O-]', 'CC(=O)[O-]'),
            ('CC(=O)[O-].[K+]', 'CC(=O)[O-]'),
            ('O.[Na+].C[C@@H](O)C(=O)[O-].[Cl-]', 'C[C@@H](O)C(=O)[O-]'),
        ):
            with self.subTest(source=source):
                request = self.request(source)
                result = check_compound(request, self.models)
                self.assertEqual(result.status, 'assessed', result)
                baseline = check_compound(self.request(isolated, shifts=request.carbon_shifts), self.models)
                self.assertEqual(baseline.status, 'assessed', baseline)
                self.assertEqual(result.score_raw, baseline.score_raw)
                self.assertEqual(sorted(label for row in result.assignments for label in row['atom_labels']),
                                 sorted(request.carbon_labels))
                scope = result.provenance['component_scope']
                self.assertEqual(scope['original_smiles'], source)
                self.assertEqual(scope['scored_fragment_smiles'], Chem.MolToSmiles(Chem.MolFromSmiles(isolated)))
                self.assertTrue(scope['ignored_components'])
                self.assertIn(COMPONENT_WARNING, result.diagnostics['scope_note'])

    def test_multiple_carbon_components_refuse_without_geometry_or_peak_allocation(self):
        for source in ('CC[NH3+].[O-]C(=O)C(F)(F)F', 'C[NH3+].CC(=O)[O-]',
                       'CCO.CCC', 'O.CC.O.CCO'):
            with self.subTest(source=source), patch('checksi.dp5q.engine.prepare_geometry',
                    side_effect=AssertionError('mixtures must not prepare geometry')):
                result = check_compound(self.request(source, shifts=[20.]), self.models)
                self.assertEqual((result.status, result.reason), ('not_applicable', 'multiple_carbon_components'))
                self.assertEqual(result.diagnostics['message'], MULTIPLE_COMPONENTS)
                self.assertEqual(result.assignments, [])
                self.assertIsNone(result.score_raw)

    def test_multiple_carbon_component_preparation_is_exact_info_before_worker_start(self):
        config = review_config()
        for source in ('CC[NH3+].[O-]C(=O)C(F)(F)F', 'CCO.CC(=O)O'):
            compound = {'id': 1, 'smiles': source, 'run_dp5': True,
                        'dp5_c_signals_list': [20., 30.], 'dp5_carbon_groups': []}
            with self.subTest(source=source):
                skip, = prepare_dp5_requests(config, {'compounds': [compound]})
                self.assertIsInstance(skip, DP5PreparationSkip)
                self.assertEqual(skip.reason, 'multiple_carbon_components')
                self.assertEqual(skip.info_message, MULTIPLE_COMPONENTS)

    def test_component_group_limit_is_a_skip_and_does_not_abort_preparing_later_compounds(self):
        compounds = [
            {'id': 1, 'smiles': '[Na+].' + 'C' * 129 + 'O', 'run_dp5': False,
             'dp5_c_signals_list': [20.], 'dp5_carbon_groups': [],
             'dp5_refusal_details': {'cause': 'group_limit', 'required_count': 129}},
            {'id': 2, 'smiles': 'CCO', 'run_dp5': True,
             'dp5_c_signals_list': [20., 60.], 'dp5_carbon_groups': []},
        ]
        first, second = prepare_dp5_requests(review_config(), {'compounds': compounds})
        self.assertIsInstance(first, DP5PreparationSkip)
        self.assertEqual(first.reason, 'group_limit')
        self.assertIsInstance(second, CheckRequest)
        with patch('checksi.dp5q.client.subprocess.Popen',
                   side_effect=AssertionError('out-of-domain component must not start a worker')):
            result = enrich_analysis_with_dp5(review_config(), {'compounds': compounds[:1]})
        self.assertEqual(result['dp5_summary']['skipped'], 1)
        self.assertEqual(result['dp5_summary']['failed'], 0)

    def test_all_source_boron_skip_precedes_organic_component_selection(self):
        source = 'CC[NH3+].F[B-](F)(F)F'
        request = self.request(source)
        result = check_compound(request, self.models)
        self.assertEqual(result.reason, 'boron_not_supported')
        compound = {'id': 1, 'smiles': source, 'run_dp5': True,
                    'dp5_c_signals_list': list(request.carbon_shifts), 'dp5_carbon_groups': []}
        skip, = prepare_dp5_requests(review_config(), {'compounds': [compound]})
        self.assertIsInstance(skip, DP5PreparationSkip)
        self.assertEqual(skip.reason, 'boron_not_supported')

    def test_fragment_mapping_preserves_interleaved_source_atom_indices(self):
        molecule = Chem.RenumberAtoms(Chem.MolFromSmiles('CCO.O.[Na+]'), [0, 3, 1, 4, 2])
        before = molecule.ToBinary()
        component = select_scoring_component(molecule)
        self.assertEqual(component.original_atom_indices, (0, 2, 4))
        self.assertEqual(component.original_labels((0, 1)), ('C1', 'C3'))
        self.assertEqual(component.project_groups([['C1', 'C3']]), (('C1', 'C2'),))
        self.assertEqual(Chem.MolToSmiles(component.mol), 'CCO')
        self.assertEqual(molecule.ToBinary(), before)

    def test_multi_carbon_and_boron_preparation_never_start_a_worker(self):
        for source, reason in (('CCN.CC(=O)O', 'multiple_carbon_components'),
                               ('CCN.F[B-](F)(F)F', 'boron_not_supported')):
            compound = {'id': 1, 'smiles': source, 'run_dp5': True,
                        'dp5_c_signals_list': [20., 30.], 'dp5_carbon_groups': []}
            with self.subTest(source=source), patch('checksi.dp5q.client.subprocess.Popen',
                    side_effect=AssertionError('unscored input must not start a worker')):
                result = enrich_analysis_with_dp5(review_config(), {'compounds': [compound]})
                self.assertEqual(compound['dp5']['reason'], reason)
                self.assertEqual(result['dp5_summary']['skipped'], 1)
                self.assertNotIn('score_raw', compound['dp5'])
                if reason == 'multiple_carbon_components':
                    self.assertEqual(compound['dp5']['message'], MULTIPLE_COMPONENTS)
                    self.assertEqual(compound['dp5']['message_severity'], 'info')

    def test_native_disconnected_groups_are_replaced_only_in_the_prepared_request(self):
        source = '[K+].CC(C)OC(=O)[C@H](O)[C@@H](O)C(=O)OC(C)C'
        original = self.request(source)
        compound = {'id': 1, 'smiles': source, 'run_dp5': False,
                    'dp5_c_signals_list': [170., 100., 80., 30., 20.],
                    'dp5_carbon_groups': [list(group) for group in original.carbon_groups],
                    'dp5_refusal_details': {'cause': 'count_mismatch', 'required_count': 7}}
        request, = prepare_dp5_requests(review_config(), {'compounds': [compound]})
        self.assertIsInstance(request, CheckRequest)
        self.assertEqual(request.smiles, source)
        self.assertEqual(request.carbon_labels, original.carbon_labels)
        self.assertNotEqual(request.carbon_groups, original.carbon_groups)
        self.assertEqual(compound['dp5_carbon_groups'], [list(group) for group in original.carbon_groups])
        result = check_compound(request, self.models)
        self.assertEqual(result.status, 'assessed', result)
        self.assertEqual(len(result.assignments), 5)
        # Explicit engine requests are bound exactly to their supplied groups.
        unnormalized = check_compound(original.model_copy(update={'carbon_shifts': request.carbon_shifts}), self.models)
        self.assertEqual(unnormalized.reason, 'source_partition_mismatch')

    def test_a_salt_cannot_authorize_invented_groups_or_hide_malformed_annotations(self):
        for groups, cause in (([['C2', 'C3']], 'source_partition_mismatch'),
                               ([], 'invalid_carbon_annotation')):
            compound = {'id': 1, 'smiles': '[Na+].CC(=O)[O-]', 'run_dp5': False,
                        'dp5_c_signals_list': [20., 170.], 'dp5_carbon_groups': groups,
                        'dp5_refusal_details': {'cause': cause}}
            with self.subTest(cause=cause):
                skip, = prepare_dp5_requests(review_config(), {'compounds': [compound]})
                self.assertIsInstance(skip, DP5PreparationSkip)
                self.assertEqual(skip.reason, cause)


if __name__ == '__main__':
    unittest.main()
