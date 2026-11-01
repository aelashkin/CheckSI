"""Incomplete structural work must not revive lower symmetry partitions."""

import unittest
from unittest.mock import patch

from rdkit import Chem

from checksi.analysis.structure_analysis import analyze_molecular_structure_detailed


class StructureFallbackTests(unittest.TestCase):
    def test_bounded_embedding_failure_marks_partition_incomplete_and_lower_options_manual(self):
        with patch('checksi.analysis.structure_analysis.rdDistGeom.EmbedMolecule', return_value=-1):
            result = analyze_molecular_structure_detailed(Chem.MolFromSmiles('CC(C)[C@H](N)C(=O)O'))
        self.assertEqual(result.carbon_count, 5)
        self.assertFalse(result.partition_complete)
        self.assertEqual(result.partition_reason, 'substitution_incomplete')
        lower = [a for a in result.c13_alternatives if a.count < result.carbon_count]
        self.assertTrue(lower)
        self.assertTrue(all(a.manual_check_only and not a.groups_dp5 for a in lower))

    def test_inverted_pinene_public_result_is_inadmissible_with_no_lower_scoring_partition(self):
        molecule = Chem.MolFromSmiles('CC=1C2C(C(CC1)C2)(C)C')
        molecule.GetAtomWithIdx(2).SetChiralTag(Chem.ChiralType.CHI_TETRAHEDRAL_CW)
        molecule.GetAtomWithIdx(4).SetChiralTag(Chem.ChiralType.CHI_TETRAHEDRAL_CCW)
        result = analyze_molecular_structure_detailed(molecule)
        self.assertEqual(result.configuration_status, 'inadmissible')
        self.assertFalse(result.partition_complete)
        self.assertEqual(result.partition_reason, 'physically_inadmissible')
        lower = [a for a in result.c13_alternatives if a.count < result.carbon_count]
        self.assertTrue(all(a.manual_check_only and not a.groups_dp5 for a in lower))


if __name__ == '__main__':
    unittest.main()
