"""Bridge topology, rather than embedding failure, excludes inverted pinene states."""

import unittest
from unittest.mock import patch

from rdkit import Chem

from checksi.analysis import structure_analysis
from test_structure_expert_oracle import CASES


class PhysicalIsomerTests(unittest.TestCase):
    def enumerate(self, molecule):
        helper = getattr(structure_analysis, 'enumerate_supported_stereoisomers', None)
        self.assertIsNotNone(helper, 'Physical stereo enumeration is absent.')
        return helper(molecule)

    def test_unspecified_pinene_has_only_the_physical_enantiomer_pair(self):
        original = Chem.MolFromSmiles(CASES['alpha_pinene']['smiles'])
        expected = {'CC1=CC[C@H]2C[C@@H]1C2(C)C', 'CC1=CC[C@@H]2C[C@H]1C2(C)C'}
        for molecule in (original, Chem.AddHs(original), Chem.RenumberAtoms(
                original, list(reversed(range(original.GetNumAtoms()))))):
            with self.subTest(atoms=molecule.GetNumAtoms()), patch(
                    'checksi.analysis.structure_analysis.rdDistGeom.EmbedMolecule',
                    side_effect=AssertionError('A failed embedding is not an impossibility proof.')):
                result = self.enumerate(molecule)
                self.assertTrue(result.complete, result.reason)
                self.assertEqual(result.rejected_isomer_count, 2)
                self.assertEqual({Chem.MolToSmiles(Chem.RemoveHs(m), isomericSmiles=True)
                                  for m in result.isomers}, expected)

    def test_explicit_inverted_bridge_is_rejected_without_embedding(self):
        case = CASES['alpha_pinene']
        for tags in case['inadmissible_tetrahedral_tags']:
            molecule = Chem.MolFromSmiles(case['smiles'])
            for index, orientation in tags.items():
                molecule.GetAtomWithIdx(int(index)).SetChiralTag(
                    getattr(Chem.ChiralType, 'CHI_TETRAHEDRAL_' + orientation))
            with self.subTest(tags=tags):
                result = self.enumerate(molecule)
                self.assertTrue(result.complete)
                self.assertEqual(result.isomers, ())
                self.assertEqual(result.reason, 'physically_inadmissible')
                self.assertEqual(result.rejected_isomer_count, 1)


if __name__ == '__main__':
    unittest.main()
