"""Permitted coincidence is separate from complete chemical environments."""

import unittest

from rdkit import Chem

from checksi.analysis import structure_analysis
from test_structure_expert_oracle import CASES, analyze_case


class CoincidenceCandidateTests(unittest.TestCase):
    def candidates(self, molecule, groups):
        helper = getattr(structure_analysis, 'isopropyl_methyl_coincidences', None)
        self.assertIsNotNone(helper, 'Typed structural coincidence candidate helper is absent.')
        return helper(molecule, groups)

    def test_justified_isopropyl_pairs_retain_whole_formal_environments(self):
        expected = {
            'L_valine': (((0,), (2,)), (1,)),
            'S_isopropyl_methyl_sulfoxide': (((4,), (5,)), (3,)),
            'diisopropyl_L_tartrate': (((0, 15), (2, 14)), (1, 13)),
            'prochiral_OH': (((0, 7), (2, 6)), (1, 5)),
            'prochiral_OEt': (((0, 9), (2, 8)), (1, 7)),
            'prochiral_OAc': (((0, 10), (2, 9)), (1, 8)),
            'prochiral_Oallyl': (((0, 10), (2, 9)), (1, 8)),
        }
        for identifier, (environments, methines) in expected.items():
            case = CASES[identifier]
            for molecule in (Chem.MolFromSmiles(case['smiles']), Chem.AddHs(Chem.MolFromSmiles(case['smiles']))):
                with self.subTest(case=identifier, atoms=molecule.GetNumAtoms()):
                    actual = self.candidates(molecule, case['groups'])
                    self.assertEqual(len(actual), 1)
                    self.assertEqual(actual[0].environments, environments)
                    self.assertEqual(actual[0].methine_atoms, methines)
                    self.assertEqual(actual[0].reason, 'isopropyl_methyl_coincidence')
                    self.assertTrue(all(list(group) in case['groups'] for group in environments))
            assessed = analyze_case(case)
            self.assertEqual(assessed.coincidence_candidates[0].environments, environments)
            self.assertEqual(assessed.coincidence_candidates[0].methine_atoms, methines)

    def test_manual_count_alone_never_admits_gem_dimethyl_or_ring_coincidence(self):
        for identifier in ('1.142', '1.145', '1.147', 'alpha_pinene'):
            case = CASES[identifier]
            with self.subTest(case=identifier):
                self.assertEqual(self.candidates(Chem.MolFromSmiles(case['smiles']), case['groups']), ())
        self.assertEqual(self.candidates(Chem.MolFromSmiles('CC(C)O'), [[0, 2], [1]]), ())

    def test_candidate_input_requires_complete_disjoint_original_carbon_coverage(self):
        molecule = Chem.MolFromSmiles(CASES['L_valine']['smiles'])
        for groups in ([[0], [1], [2], [3]], [[0], [0], [1], [2], [3], [5]],
                       [[True], [0], [2], [3], [5]]):
            with self.subTest(groups=groups), self.assertRaises(ValueError):
                self.candidates(molecule, groups)


if __name__ == '__main__':
    unittest.main()
