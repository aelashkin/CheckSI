"""Computable stereostructures retain scores with honest verification scope."""

import unittest
from unittest.mock import patch

from rdkit import Chem

from checksi.analysis.structure_analysis import analyze_molecular_structure_detailed
from checksi.controller import CheckSIController
from checksi.dp5_enrichment import enrich_analysis_with_dp5
from checksi.dp5q.contracts import CheckRequest, validate_result_for_request
from checksi.dp5q.engine import check_compound
from checksi.dp5q.geometry import prepare_geometry
from checksi.dp5q.network import load_models
from tests.test_dp5_review_fixes import review_config


WARNING = 'DP5q calculated for one stereoisomer only; another isomer may give a very different result.'


class ComputableStereoScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.models = load_models()
        metadata = cls.models.metadata
        cls.ready = {
            'capabilities': ['coincident-observations-v1'], 'model_id': metadata['model_id'],
            'preprocessing_id': metadata['preprocessing_id'], 'score_version': metadata['score_id'],
            'geometry_policy': metadata['geometry_policy'],
            'weights_sha256': metadata['files']['weights.npz']['sha256'],
        }

    def assess(self, smiles):
        source = analyze_molecular_structure_detailed(Chem.MolFromSmiles(smiles))
        request = CheckRequest(compound_id='stereo', smiles=smiles, carbon_groups=source.carbon_groups,
                               carbon_shifts=[float(20 + 10*i) for i in range(source.carbon_count)])
        result = check_compound(request, self.models)
        self.assertEqual(result.status, 'assessed', result)
        validate_result_for_request(request, result, self.ready)
        self.assertEqual(sorted(int(label[1:]) for row in result.assignments for label in row['atom_labels']),
                         [atom.GetIdx() + 1 for atom in Chem.MolFromSmiles(smiles).GetAtoms()
                          if atom.GetAtomicNum() == 6])
        return source, result

    def test_embedding_scope_accepts_unperceivable_n_p_and_allene_elements(self):
        for smiles in (
            'CN1CC1C(O)C(F)C(Cl)C(Br)C',
            'CP(CC)C(O)C(F)C(Cl)C(Br)C',
            'CC=C=CC(O)C(F)C(Cl)C(Br)C',
        ):
            with self.subTest(smiles=smiles):
                source, result = self.assess(smiles)
                self.assertFalse(source.partition_complete)
                self.assertFalse(result.provenance['stereo_verification_complete'])
                self.assertFalse(result.provenance['coordinate_partition_verified'])
                self.assertTrue(result.provenance['configuration_dependent'])
                self.assertIn(WARNING, result.diagnostics['scope_note'])

    def test_unembeddable_enumerated_forms_do_not_refuse_computable_bicyclics(self):
        for smiles in ('CC1(C2CCC1(C(=O)C2)C)C', 'CC1(C2CCC1(C(O)C2)C)C',
                       'CC1(C2CCC(O1)(CC2)C)C'):
            with self.subTest(smiles=smiles):
                source, result = self.assess(smiles)
                self.assertFalse(source.partition_complete)
                self.assertEqual(source.partition_reason, 'substitution_incomplete')
                self.assertEqual(result.provenance['partition_reason'], source.partition_reason)
                self.assertIn(WARNING, result.diagnostics['scope_note'])

    def test_specified_three_coordinate_n_and_p_score_without_claiming_verification(self):
        for smiles in ('C[N@]1C(C)C1', 'CCC[P@](C)c1ccccc1'):
            with self.subTest(smiles=smiles):
                _, result = self.assess(smiles)
                self.assertFalse(result.provenance['stereo_verification_complete'])
                self.assertFalse(result.provenance['coordinate_partition_verified'])
                self.assertNotIn('configuration_dependent', result.provenance)
                self.assertIn('cannot be verified from 3D coordinates', result.diagnostics['scope_note'])

    def test_enhanced_stereo_scores_one_form_without_a_complete_partition_claim(self):
        source, result = self.assess('C[C@H](O)[C@H](O)C |&1:1,3|')
        self.assertFalse(source.partition_complete)
        self.assertEqual(source.partition_reason, 'unsupported_representation')
        self.assertFalse(result.provenance['stereo_verification_complete'])
        self.assertFalse(result.provenance['coordinate_partition_verified'])
        self.assertIn(WARNING, result.diagnostics['scope_note'])

    def test_coordinate_partition_proof_does_not_filter_the_lowest_energy_geometry(self):
        smiles = 'COc1ccc2nccc([C@@H](O)[C@@H]3C[C@@H]4CCN3C[C@@H]4C=C)c2c1'
        geometry = prepare_geometry(smiles)
        _, result = self.assess(smiles)
        self.assertEqual(result.diagnostics['energies_kj'], geometry.energies_kj.tolist())
        self.assertFalse(result.provenance['coordinate_partition_verified'])
        self.assertFalse(result.provenance['stereo_verification_complete'])

    def test_parent_rejects_a_false_complete_stereo_verification_claim(self):
        smiles = 'C[N@]1C(C)C1'
        source, result = self.assess(smiles)
        request = CheckRequest(compound_id='stereo', smiles=smiles, carbon_groups=source.carbon_groups,
                               carbon_shifts=[float(20 + 10*i) for i in range(source.carbon_count)])
        forged = result.model_copy(update={'provenance': {**result.provenance,
                                                         'stereo_verification_complete': True}})
        with self.assertRaisesRegex(ValueError, 'stereo verification scope'):
            validate_result_for_request(request, forged, self.ready)

    def test_real_session_product_path_keeps_incomplete_partition_score_and_warning(self):
        config = review_config()
        for smiles in ('CN1CC1C(O)C(F)C(Cl)C(Br)C', 'CC1(C2CCC1(C(=O)C2)C)C'):
            with self.subTest(smiles=smiles):
                source = analyze_molecular_structure_detailed(Chem.MolFromSmiles(smiles))
                shifts = ', '.join(f'{20. + 10*i:.1f}' for i in range(source.carbon_count))
                description = ('1H NMR (400 MHz, CDCl3): δ 1.20 (s, 6H). '
                               f'13C NMR (100 MHz, CDCl3): δ {shifts}.')
                with patch('checksi.controller._smiles_and_name_from_paragraph',
                           return_value=(smiles, 'Compound', '1', {}, [])):
                    analysis = CheckSIController(config).run_analysis(['Compound (1)', description])
                self.assertTrue(analysis['compounds'][0]['run_dp5'])
                result = enrich_analysis_with_dp5(config, analysis)
                compound = result['compounds'][0]
                self.assertEqual(compound['dp5']['status'], 'success', compound['dp5'])
                self.assertIn(WARNING, ' '.join(compound['all_warnings']))
                self.assertEqual(result['dp5_summary']['failed'], 0)


if __name__ == '__main__':
    unittest.main()
