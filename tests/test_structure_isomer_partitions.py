"""Complete per-isomer substitution evidence remains in original atom indices."""

import unittest

from rdkit import Chem

from checksi.analysis.structure_analysis import analyze_molecular_structure_detailed


class IsomerPartitionTests(unittest.TestCase):
    def test_bounded_configuration_variation_retains_formal_counts(self):
        molecule = Chem.MolFromSmiles('CC(O)C(C)C(C)O')
        before = molecule.ToBinary()
        result = analyze_molecular_structure_detailed(molecule)
        self.assertTrue(result.partition_complete, result.partition_reason)
        self.assertEqual(result.expected_counts('C'), (4, 6))
        self.assertTrue(result.configuration_dependent('C'))
        self.assertGreater(len(result.isomer_partitions), 1)
        carbons = {atom.GetIdx() for atom in molecule.GetAtoms() if atom.GetAtomicNum() == 6}
        for variant in result.isomer_partitions:
            groups = variant.for_element('C').groups
            self.assertEqual({index for group in groups for index in group}, carbons)
            self.assertEqual(sum(map(len, groups)), len(carbons))
            self.assertEqual(variant.isomer.GetNumAtoms(), molecule.GetNumAtoms())
        self.assertEqual(molecule.ToBinary(), before)

    def test_explicit_H_and_renumbering_retain_each_original_inventory(self):
        molecule = Chem.MolFromSmiles('CC(O)C(C)C(C)O')
        for source in (molecule, Chem.AddHs(molecule), Chem.RenumberAtoms(
                molecule, list(reversed(range(molecule.GetNumAtoms()))))):
            with self.subTest(atoms=source.GetNumAtoms()):
                result = analyze_molecular_structure_detailed(source)
                self.assertTrue(result.partition_complete, result.partition_reason)
                self.assertEqual(result.expected_counts('C'), (4, 6))
                for variant in result.isomer_partitions:
                    self.assertEqual([atom.GetAtomicNum() for atom in variant.isomer.GetAtoms()],
                                     [atom.GetAtomicNum() for atom in source.GetAtoms()])
                    self.assertEqual([(bond.GetBeginAtomIdx(), bond.GetEndAtomIdx())
                                      for bond in variant.isomer.GetBonds()],
                                     [(bond.GetBeginAtomIdx(), bond.GetEndAtomIdx())
                                      for bond in source.GetBonds()])

    def test_beyond_eight_forms_is_unassessed(self):
        result = analyze_molecular_structure_detailed(Chem.MolFromSmiles('CC(O)C(F)C(Cl)C(Br)C'))
        self.assertFalse(result.partition_complete)
        self.assertEqual(result.partition_reason, 'enumeration_budget')
        self.assertEqual(result.isomer_partitions, ())


if __name__ == '__main__':
    unittest.main()
