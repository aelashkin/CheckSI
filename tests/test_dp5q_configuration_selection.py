"""Conditional DP5q selection is deterministic, source-framed and not score driven."""

import unittest
from unittest.mock import patch

from rdkit import Chem

from checksi.analysis.structure_analysis import analyze_molecular_structure_detailed
from checksi.config import ConfigManager
from checksi.config_schema import ErrorSeverity
from checksi.dp5_enrichment import enrich_analysis_with_dp5
from checksi.dp5q.contracts import CheckRequest, validate_result_for_request
from checksi.dp5q.engine import check_compound
from checksi.dp5q.geometry import GeometryError, prepare_geometry
from checksi.dp5q.network import load_models
from checksi.dp5q.observation_policy import derive_observation_policy


SMILES = 'CC(O)C(C)C(C)O'


class ConfigurationSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.models = load_models()
        cls.source = analyze_molecular_structure_detailed(Chem.MolFromSmiles(SMILES))

    def request(self, count, shifts=None):
        variant = next(variant for variant in self.source.isomer_partitions
                       if len(variant.for_element('C').groups) == count)
        return CheckRequest(compound_id='conditional', smiles=SMILES,
                            carbon_shifts=shifts or [float(100-10*i) for i in range(count)],
                            carbon_groups=variant.carbon_groups)

    def ready(self):
        metadata = self.models.metadata
        return {'capabilities': ['coincident-observations-v1'], 'model_id': metadata['model_id'],
                'preprocessing_id': metadata['preprocessing_id'], 'score_version': metadata['score_id'],
                'geometry_policy': metadata['geometry_policy'],
                'weights_sha256': metadata['files']['weights.npz']['sha256']}

    def test_geometry_receives_selected_Mol_in_original_atom_order_before_scoring(self):
        for count in (4, 6):
            request = self.request(count)
            policy = derive_observation_policy(Chem.MolFromSmiles(SMILES), request.carbon_groups)
            received = []
            def stop_before_inference(molecule, **kwargs):
                received.append(molecule)
                raise GeometryError('embedding_failed', 'selection boundary fixture')
            with patch('checksi.dp5q.engine.prepare_geometry', side_effect=stop_before_inference):
                result = check_compound(request, self.models)
            self.assertEqual(result.reason, 'embedding_failed')
            self.assertEqual(len(received), 1)
            self.assertEqual(Chem.MolToSmiles(Chem.RemoveHs(received[0])), policy.selected_isomer_smiles)
            self.assertEqual([atom.GetSymbol() for atom in received[0].GetAtoms()],
                             [atom.GetSymbol() for atom in Chem.MolFromSmiles(SMILES).GetAtoms()])
            self.assertEqual([(bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()) for bond in received[0].GetBonds()],
                             [(bond.GetBeginAtomIdx(), bond.GetEndAtomIdx())
                              for bond in Chem.MolFromSmiles(SMILES).GetBonds()])

    def test_noncarbon_variation_locks_one_isomer_without_inspecting_shift_values(self):
        source = 'CC(O)C(F)(F)C(O)C'
        structure = analyze_molecular_structure_detailed(Chem.MolFromSmiles(source))
        self.assertFalse(structure.configuration_dependent('C'))
        self.assertTrue(structure.configuration_dependent('F'))
        received = []
        def capture(molecule, **kwargs):
            received.append(Chem.MolToSmiles(Chem.RemoveHs(molecule)))
            raise GeometryError('embedding_failed', 'selection boundary fixture')
        for shifts in ([10., 20., 30.], [200., 100., -10.]):
            request = CheckRequest(compound_id='F-only', smiles=source,
                                   carbon_shifts=shifts, carbon_groups=structure.carbon_groups)
            with patch('checksi.dp5q.engine.prepare_geometry', side_effect=capture):
                result = check_compound(request, self.models)
            self.assertEqual(result.reason, 'embedding_failed')
        self.assertEqual(received, [structure.isomer_partitions[0].smiles] * 2)

    def test_other_admissible_same_partition_isomer_cannot_reach_predictions(self):
        request = self.request(6)
        policy = derive_observation_policy(Chem.MolFromSmiles(SMILES), request.carbon_groups)
        other = next(variant for variant in self.source.isomer_partitions
                     if len(variant.for_element('C').groups) == 6 and variant.smiles != policy.selected_isomer_smiles)
        wrong = prepare_geometry(other.isomer)
        with patch('checksi.dp5q.engine.prepare_geometry', return_value=wrong), patch.object(
                self.models, 'predict', side_effect=AssertionError('wrong isomer reached predictions')):
            result = check_compound(request, self.models)
        self.assertEqual(result.status, 'not_applicable')
        self.assertEqual(result.reason, 'stereochemistry_changed')

    def test_real_assessment_owns_selection_and_parent_binds_the_source_graph(self):
        request = self.request(4).model_copy(update={'fixed_assignments': {index: index for index in range(4)}})
        result = check_compound(request, self.models)
        self.assertEqual(result.status, 'assessed', result)
        validate_result_for_request(request, result, self.ready())
        self.assertTrue(result.provenance['configuration_dependent'])
        self.assertEqual(result.provenance['expected_carbon_counts'], [4, 6])
        self.assertEqual(result.provenance['isomer_selection_policy'], 'first-compatible-original-partition-v1')
        wrong = next(variant.smiles for variant in self.source.isomer_partitions
                     if variant.smiles != result.provenance['selected_isomer_smiles'])
        forged = result.model_copy(update={'provenance': {**result.provenance, 'selected_isomer_smiles': wrong}})
        # Choosing the deterministic source-compatible form belongs to the worker;
        # the host binds the returned graph without repeating stereo enumeration.
        with patch('checksi.analysis.structure_analysis.analyze_molecular_structure_detailed',
                   side_effect=AssertionError('host repeated source analysis')):
            validate_result_for_request(request, forged, self.ready())

    def test_invariant_result_cannot_forge_a_configuration_dependent_warning(self):
        request = CheckRequest(compound_id='invariant', smiles='CCO', carbon_shifts=[18., 58.])
        result = check_compound(request, self.models)
        self.assertEqual(result.status, 'assessed')
        validate_result_for_request(request, result, self.ready())
        for key, value in (('configuration_dependent', True), ('expected_carbon_counts', [1, 2, 3]),
                           ('source_smiles', 'UNRELATED'), ('isomer_selection_policy', 'invented')):
            with self.subTest(key=key):
                forged = result.model_copy(update={'provenance': {**result.provenance, key: value}})
                with self.assertRaises(ValueError):
                    validate_result_for_request(request, forged, self.ready())

    def test_false_grouping_and_overbudget_count_mismatch_refuse_before_geometry(self):
        requests = [self.request(4).model_copy(update={'carbon_groups': (('C1', 'C2'), ('C6', 'C7'))}),
                    CheckRequest(compound_id='budget', smiles='CC(O)C(F)C(Cl)C(Br)C', carbon_shifts=[20.])]
        for request, reason in zip(requests, ('source_partition_mismatch', 'strict_nmr_coverage_mismatch')):
            with self.subTest(reason=reason), patch('checksi.dp5q.engine.prepare_geometry',
                    side_effect=AssertionError('unsupported source reached geometry')):
                result = check_compound(request, self.models)
            self.assertEqual((result.status, result.reason), ('not_applicable', reason))
            self.assertEqual(result.assignments, [])

    def test_enumeration_budget_scores_embedded_form_and_parent_validates_scope(self):
        smiles = 'C[C@H](O)C(F)C(Cl)C(Br)C(O)C'
        molecule = Chem.MolFromSmiles(smiles)
        source = analyze_molecular_structure_detailed(molecule)
        self.assertEqual(source.partition_reason, 'enumeration_budget')
        request = CheckRequest(compound_id='budget', smiles=smiles,
            carbon_shifts=[float(20 + 10 * index) for index in range(source.carbon_count)],
            carbon_groups=source.carbon_groups)
        result = check_compound(request, self.models)
        self.assertEqual(result.status, 'assessed', result)
        validate_result_for_request(request, result, self.ready())
        self.assertTrue(result.provenance['configuration_dependent'])
        self.assertFalse(result.provenance['coordinate_partition_verified'])
        self.assertEqual(result.provenance['partition_reason'], 'enumeration_budget')
        self.assertEqual(result.provenance['isomer_selection_policy'], 'lowest-energy-embedded-isomer-v1')
        for selected in ('CCO', smiles):
            forged = result.model_copy(update={'provenance': {**result.provenance,
                                                              'selected_isomer_smiles': selected}})
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                validate_result_for_request(request, forged, self.ready())
        opposite = Chem.MolFromSmiles(result.provenance['selected_isomer_smiles'])
        for atom in opposite.GetAtoms():
            atom.InvertChirality()
        forged = result.model_copy(update={'provenance': {**result.provenance,
            'selected_isomer_smiles': Chem.MolToSmiles(opposite)}})
        with self.assertRaises(ValueError):
            validate_result_for_request(request, forged, self.ready())

    def test_enumeration_budget_cannot_authorize_invented_source_groups(self):
        request = CheckRequest(compound_id='false-budget-groups',
            smiles='C[C@H](O)C(F)C(Cl)C(Br)C(O)C',
            carbon_groups=[['C1', 'C2']], carbon_shifts=[20., 30., 40., 50., 60., 70.])
        with patch('checksi.dp5q.engine.prepare_geometry',
                   side_effect=AssertionError('false source groups reached geometry')):
            result = check_compound(request, self.models)
        self.assertEqual((result.status, result.reason), ('not_applicable', 'source_partition_mismatch'))

    def test_unused_representative_group_limit_does_not_block_selected_partition(self):
        request = self.request(4)
        with patch('checksi.dp5q.assignment.MAX_GROUPS', 5), patch(
                'checksi.dp5q.engine.prepare_geometry',
                side_effect=GeometryError('embedding_failed', 'selection boundary fixture')) as prepare:
            result = check_compound(request, self.models)
        prepare.assert_called_once()
        self.assertEqual(result.reason, 'embedding_failed')

    def test_conditional_score_warning_has_independent_visibility_and_keeps_score(self):
        request = self.request(4).model_copy(update={'compound_id': '1',
                                                    'fixed_assignments': {index: index for index in range(4)}})
        result = check_compound(request, self.models)
        self.assertEqual(result.status, 'assessed', result)
        for disabled in (False, True):
            config = ConfigManager.load_factory()
            config.dp5.enabled = True
            if disabled:
                config.errors.overrides['dp5_configuration_dependent'] = ErrorSeverity.DISABLED
            compound = {'id': 1, 'smiles': SMILES, 'all_warnings': [],
                        'run_dp5': True, 'dp5_carbon_groups': [list(group) for group in request.carbon_groups],
                        'dp5_c_signals_list': list(request.carbon_shifts),
                        'fixed_assignments': dict(request.fixed_assignments)}
            with patch('checksi.dp5_enrichment.DP5qSession') as session:
                active = session.return_value.__enter__.return_value
                active.metadata = self.ready()
                active.check.return_value = result
                enriched = enrich_analysis_with_dp5(config, {'compounds': [compound], 'error_stats': {}})
            self.assertEqual(compound['dp5']['score_raw'], result.score_raw)
            self.assertEqual(enriched['error_stats']['dp5_configuration_dependent'], int(not disabled))
            self.assertEqual(sum('another isomer may give a very different result' in note
                                 for note in compound['all_warnings']), int(not disabled))


if __name__ == '__main__':
    unittest.main()
