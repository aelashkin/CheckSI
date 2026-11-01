"""Existing-coordinate substitution partitions retain original atom identities."""

import unittest
from unittest.mock import patch

from rdkit import Chem

from checksi.analysis import structure_analysis
from test_structure_expert_oracle import CASES, ORACLE, frame_embedder, partition


class CoordinatePartitionTests(unittest.TestCase):
    def measure(self, molecule):
        helper = getattr(structure_analysis, 'carbon_partition_from_coordinates', None)
        self.assertIsNotNone(helper, 'Existing-coordinate partition helper is absent.')
        return helper(molecule)

    def fixture(self, identifier):
        case = CASES[identifier]
        molecule = Chem.AddHs(Chem.MolFromSmiles(case['smiles']))
        frame_embedder(ORACLE['reference_frames'][case['frame']],
                       list(range(molecule.GetNumAtoms())))(molecule, None)
        return molecule

    def test_hand_frames_preserve_exact_partitions_without_reembedding(self):
        for identifier in ('diisopropyl_L_tartrate', 'prochiral_OH', 'prochiral_OEt',
                           'prochiral_OAc', 'prochiral_Oallyl'):
            with self.subTest(case=identifier):
                molecule = self.fixture(identifier)
                before = molecule.ToBinary()
                with patch('checksi.analysis.structure_analysis.rdDistGeom.EmbedMolecule',
                           side_effect=AssertionError('Existing coordinates must not be reembedded.')):
                    result = self.measure(molecule)
                self.assertTrue(result.complete, result.reason)
                self.assertEqual(partition(result.groups), partition(CASES[identifier]['groups']))
                self.assertEqual(molecule.ToBinary(), before)

    def test_explicit_permutation_transports_back_to_original_indices(self):
        molecule = self.fixture('diisopropyl_L_tartrate')
        order = list(reversed(range(molecule.GetNumAtoms())))
        result = self.measure(Chem.RenumberAtoms(molecule, order))
        self.assertTrue(result.complete, result.reason)
        original = [[order[index] for index in group] for group in result.groups]
        self.assertEqual(partition(original), partition(CASES['diisopropyl_L_tartrate']['groups']))

    def test_missing_coordinates_are_unassessed(self):
        result = self.measure(Chem.MolFromSmiles('CCO'))
        self.assertFalse(result.complete)
        self.assertEqual(result.reason, 'missing_geometry')
        self.assertEqual(result.groups, ())

    def test_degenerate_coordinates_cannot_certify_a_partition(self):
        molecule = self.fixture('prochiral_OAc')
        for index in range(molecule.GetNumAtoms()):
            molecule.GetConformer().SetAtomPosition(index, (0., 0., 0.))
        result = self.measure(molecule)
        self.assertFalse(result.complete)
        self.assertEqual(result.reason, 'coordinate_assessment_failed')
        self.assertEqual(result.groups, ())

    def test_two_dimensional_and_nonfinite_coordinates_are_unassessed(self):
        molecule = self.fixture('prochiral_OAc')
        molecule.GetConformer().Set3D(False)
        result = self.measure(molecule)
        self.assertFalse(result.complete)
        self.assertEqual(result.reason, 'not_3d_geometry')
        molecule.GetConformer().Set3D(True)
        molecule.GetConformer().SetAtomPosition(0, (float('nan'), 0., 0.))
        result = self.measure(molecule)
        self.assertFalse(result.complete)
        self.assertEqual(result.reason, 'nonfinite_geometry')
        self.assertEqual(result.groups, ())


if __name__ == '__main__':
    unittest.main()
