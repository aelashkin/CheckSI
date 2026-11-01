"""Value-level checks for the structure-to-DP5 atom label producer."""

import unittest

from rdkit import Chem

from checksi.analysis.structure_analysis import converter_base_groups_to_dp5


class StructureGroupTests(unittest.TestCase):
    """Atom labels follow full molecule indices, including heteroatoms."""

    def test_labels_are_one_based_filtered_and_sorted(self):
        mol = Chem.MolFromSmiles("CCOCC")
        groups = {"right": [3, 1], "oxygen": [2], "ends": [4, 0]}
        self.assertEqual(converter_base_groups_to_dp5(mol, groups), [["C1", "C5"], ["C2", "C4"]])

    def test_requested_element_and_singletons_keep_original_indices(self):
        mol = Chem.MolFromSmiles("COCO")
        groups = {"mixed": [3, 0, 1], "single": [2]}
        self.assertEqual(converter_base_groups_to_dp5(mol, groups), [])
        self.assertEqual(converter_base_groups_to_dp5(mol, groups, include_singletons=True), [["C1"], ["C3"]])
        self.assertEqual(converter_base_groups_to_dp5(mol, groups, element_symbol="O"), [["O2", "O4"]])
