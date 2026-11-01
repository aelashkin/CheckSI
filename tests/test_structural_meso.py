"""Meso assessment is structural, bounded and independent of display state."""

import unittest
from unittest.mock import patch

from rdkit import Chem

from checksi.analysis.structure_analysis import analyze_molecular_structure_detailed


class StructuralMesoTests(unittest.TestCase):
    def test_enumeration_failure_is_unassessed_without_a_spectral_fallback(self):
        with patch('checksi.analysis.structure_analysis.GetStereoisomerCount',
                   side_effect=RuntimeError('enumeration failed')):
            result = analyze_molecular_structure_detailed(Chem.MolFromSmiles('CC(O)C(O)C'))
        self.assertEqual(result.stereo_assessment.status, 'unassessed')
        self.assertFalse(result.stereo_assessment.enumeration_complete)
        self.assertEqual(result.stereo_assessment.reason, 'stereo_assessment_failed')
        self.assertFalse(result.partition_complete)

    def test_assessment_retains_input_and_descriptor_roles(self):
        molecule = Chem.MolFromSmiles('C[C@H]1CC[C@@H](C)CC1')
        before = molecule.ToBinary()
        result = analyze_molecular_structure_detailed(molecule)
        self.assertEqual(molecule.ToBinary(), before)
        self.assertEqual(result.stereocenter_count, 2)
        self.assertEqual(result.stereo_assessment.rs_center_count, 0)
        self.assertEqual({element.descriptor_role for element in result.stereo_assessment.elements},
                         {'relative_ring'})


if __name__ == '__main__':
    unittest.main()
