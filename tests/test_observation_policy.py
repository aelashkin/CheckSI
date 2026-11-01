"""Original-atom policy consumers use the reviewed native04 producer frame."""

from dataclasses import replace
import unittest
from unittest.mock import patch

from rdkit import Chem

from checksi.analysis.structure_analysis import analyze_molecular_structure_detailed
from checksi.dp5q.assignment import AssignmentError
from checksi.dp5q.observation_policy import (BORON_REASON, BORON_WARNING, derive_boron_diagnostic_policy,
                                                derive_observation_policy, has_boron)

TARTRATE = 'CC(C)OC(=O)[C@H](O)[C@@H](O)C(=O)OC(C)C'
NATIVE_TARTRATE_GROUPS = [['C1', 'C15'], ['C2', 'C14'], ['C3', 'C16'], ['C5', 'C11'], ['C7', 'C9']]


class BoronDiagnosticPolicyTests(unittest.TestCase):
    def test_intact_salt_maps_every_original_atom_and_keeps_scoring_refused(self):
        mol = Chem.MolFromSmiles('[K+].CC[B-](F)(F)F')
        before = mol.ToBinary()
        policy = derive_boron_diagnostic_policy(mol)
        self.assertEqual(mol.ToBinary(), before)
        self.assertTrue(policy.partition_complete)
        self.assertEqual(policy.source_fragments, ((0,), (1,2,3,4,5,6)))
        self.assertEqual(policy.carbon_component_original_indices, (1,2,3,4,5,6))
        self.assertEqual([group.atom_indices for group in policy.groups], [(1,), (2,)])
        self.assertEqual(policy.direct_atom_pairs, ((2,3),))
        self.assertEqual(policy.direct_environments, (1,))
        self.assertFalse(derive_observation_policy(mol).partition_complete)

    def test_fragment_view_never_erases_unresolved_or_enhanced_source_stereo(self):
        unknown = Chem.MolFromSmiles('CC=CC[B-](F)(F)F.[K+]')
        unknown.GetBondWithIdx(1).SetStereo(Chem.BondStereo.STEREOANY)
        enhanced = Chem.MolFromSmiles('C[C@H](F)C[B-](F)(F)F.[K+] |&1:1|')
        mixture = Chem.MolFromSmiles('CC[B-](F)(F)F.[K+].CCO')
        for mol in (unknown, enhanced, mixture):
            with self.subTest(smiles=Chem.MolToCXSmiles(mol)):
                before = mol.ToBinary()
                policy = derive_boron_diagnostic_policy(mol)
                self.assertFalse(policy.partition_complete)
                self.assertEqual(policy.direct_environments, ())
                self.assertIsNotNone(policy.partition_reason)
                self.assertEqual(mol.ToBinary(), before)

    def test_invalid_groups_and_cancellation_cannot_publish_fragment_policy(self):
        mol = Chem.MolFromSmiles('[K+].CC[B-](F)(F)F')
        for groups in ([['C1','C2']], [['C2','C2']], [['C2',True]], [None], 1):
            with self.subTest(groups=groups):
                with patch('checksi.dp5q.observation_policy.analyze_molecular_structure_detailed') as analyze:
                    with self.assertRaises(AssignmentError):
                        derive_boron_diagnostic_policy(mol, groups)
                    analyze.assert_not_called()
        # A complete but false merge includes both directly and remotely bound carbon.
        policy = derive_boron_diagnostic_policy(Chem.MolFromSmiles('CC[B-](F)(F)F.[K+]'), [['C1','C2']])
        self.assertFalse(policy.partition_complete)
        self.assertEqual(policy.direct_environments, ())
        def cancel():
            raise RuntimeError('cancel diagnostic')
        with self.assertRaisesRegex(RuntimeError, 'cancel diagnostic'):
            derive_boron_diagnostic_policy(mol, cancel_check=cancel)


class ObservationPolicyTests(unittest.TestCase):
    def test_oversized_and_malformed_inputs_never_reach_source_analysis(self):
        for mol, groups in (
            (Chem.MolFromSmiles('C' * 513), None),
            (Chem.MolFromSmiles('C' * 171), None),  #515 atoms after explicit-H preparation.
            (Chem.MolFromSmiles('CCO'), [['C1', True]]),
            (Chem.MolFromSmiles('CCO'), [['C1', 2]]),
            (Chem.MolFromSmiles('CCO'), [None]),
            (Chem.MolFromSmiles('CCO'), 1),
        ):
            with self.subTest(groups=groups, atoms=mol.GetNumAtoms()):
                with patch('checksi.dp5q.observation_policy.analyze_molecular_structure_detailed',
                           side_effect=AssertionError('expensive analyzer reached')) as analyze:
                    with self.assertRaises(AssignmentError):
                        derive_observation_policy(mol, groups)
                    analyze.assert_not_called()

    def test_cancellation_propagates_before_source_analysis(self):
        def cancel():
            raise RuntimeError('owned deadline control')
        with patch('checksi.dp5q.observation_policy.analyze_molecular_structure_detailed') as analyze:
            with self.assertRaisesRegex(RuntimeError, 'owned deadline control'):
                derive_observation_policy(Chem.MolFromSmiles('CCO'), cancel_check=cancel)
            analyze.assert_not_called()

    def test_prediction_domain_is_checked_before_optional_source_derivation(self):
        with patch('checksi.dp5q.observation_policy.analyze_molecular_structure_detailed',
                   side_effect=AssertionError('expensive analyzer reached')) as analyze:
            with self.assertRaises(AssignmentError) as raised:
                derive_observation_policy(Chem.MolFromSmiles('C[Si](C)(C)C'), allowed_elements={1, 6, 8})
            self.assertEqual(raised.exception.reason, 'unsupported_element')
            analyze.assert_not_called()

    def test_cancellation_after_source_derivation_prevents_publishing_policy(self):
        mol = Chem.MolFromSmiles('CCO')
        structure = analyze_molecular_structure_detailed(mol)
        analyzed = False
        def derive(_):
            nonlocal analyzed
            analyzed = True
            return structure
        def cancel():
            if analyzed:
                raise RuntimeError('cancelled during source derivation')
        with patch('checksi.dp5q.observation_policy.analyze_molecular_structure_detailed', side_effect=derive):
            with self.assertRaisesRegex(RuntimeError, 'cancelled during source derivation'):
                derive_observation_policy(mol, cancel_check=cancel)

    def test_native_named_coalitions_keep_full_environments(self):
        for smiles, count, coalition in (
            ('CC(C)[C@H](N)C(=O)O', 5, (0, 2)),
            ('C[S@](=O)C(C)C', 4, (2, 3)),
            (TARTRATE, 5, (0, 2)),
        ):
            with self.subTest(smiles=smiles):
                policy = derive_observation_policy(Chem.MolFromSmiles(smiles))
                self.assertTrue(policy.partition_complete)
                self.assertEqual(len(policy.groups), count)
                self.assertEqual(policy.coincident_environments, (coalition,))
                self.assertFalse(policy.contains_boron)

    def test_supplied_native_outer_order_is_preserved_and_coalition_is_translated(self):
        mol = Chem.MolFromSmiles(TARTRATE)
        before = derive_observation_policy(mol, NATIVE_TARTRATE_GROUPS)
        after = derive_observation_policy(mol, list(reversed(NATIVE_TARTRATE_GROUPS)))
        self.assertEqual([group.labels for group in before.groups], list(map(tuple, NATIVE_TARTRATE_GROUPS)))
        self.assertEqual([group.labels for group in after.groups], list(map(tuple, reversed(NATIVE_TARTRATE_GROUPS))))
        self.assertEqual(after.coincident_environments, ((2, 4),))

    def test_hand_frame_membership_is_not_an_outer_order_permutation_of_native_frame(self):
        hand_frame = [['C5', 'C11'], ['C7', 'C9'], ['C2', 'C14'], ['C1', 'C16'], ['C3', 'C15']]
        with self.assertRaises(AssignmentError) as raised:
            derive_observation_policy(Chem.MolFromSmiles(TARTRATE), hand_frame)
        self.assertEqual(raised.exception.reason, 'source_partition_mismatch')

    def test_false_merging_or_splitting_cannot_supply_a_full_partition(self):
        for smiles, groups in [('CC(C)[C@H](N)C(=O)O', [['C1', 'C3']]), ('CC(=O)C', [])]:
            with self.subTest(smiles=smiles), self.assertRaises(AssignmentError):
                derive_observation_policy(Chem.MolFromSmiles(smiles), groups)

    def test_exact_147_direct_site_is_diagnostic_only(self):
        policy = derive_observation_policy(Chem.MolFromSmiles('CC1(C)OB(C(=C2CCCC2)c2ccc3ccccc3c2)OC1(C)C'))
        self.assertEqual(len(policy.groups), 18)
        self.assertEqual([policy.groups[i].labels for i in policy.direct_boron_environments], [('C6',)])
        self.assertTrue(policy.contains_boron)
        self.assertEqual(BORON_REASON, 'boron_not_supported')
        self.assertEqual(BORON_WARNING,
            'DP5q not calculated: boron-containing compounds are not supported (no validated geometry/prediction for boron)')

    def test_direct_sites_exclude_remote_carbons_and_count_symmetric_sites_once(self):
        remote = derive_observation_policy(Chem.MolFromSmiles('BCCC'))
        self.assertEqual([remote.groups[i].labels for i in remote.direct_boron_environments], [('C2',)])
        symmetric = derive_observation_policy(Chem.MolFromSmiles('CB(C)C'))
        self.assertEqual(len(symmetric.direct_boron_environments), 1)
        self.assertEqual(symmetric.groups[symmetric.direct_boron_environments[0]].labels, ('C1', 'C3', 'C4'))

    def test_boron_presence_does_not_require_carbon_or_geometry(self):
        self.assertTrue(has_boron(Chem.MolFromSmiles('F[B-](F)(F)F')))
        self.assertFalse(has_boron(Chem.MolFromSmiles('CCO')))

    def test_incomplete_parent_partition_cannot_justify_sharing_or_missing_sites(self):
        mol = Chem.MolFromSmiles(TARTRATE)
        structure = replace(analyze_molecular_structure_detailed(mol), partition_complete=False,
                            partition_reason='independent incomplete control')
        policy = derive_observation_policy(mol, structure=structure)
        self.assertFalse(policy.partition_complete)
        self.assertEqual(policy.partition_reason, 'independent incomplete control')
        self.assertEqual(policy.coincident_environments, ())
        self.assertEqual(policy.direct_boron_environments, ())


if __name__ == '__main__':
    unittest.main()
