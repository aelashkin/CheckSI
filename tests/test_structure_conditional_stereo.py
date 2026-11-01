"""Conditional residual elements require complete exact identity, never mirror merging."""

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from rdkit import Chem

from checksi.analysis import structure_analysis
from test_structure_expert_oracle import CASES, partition


class ConditionalStereoTests(unittest.TestCase):
    def fixture(self):
        data = json.loads((Path(__file__).parent / 'fixtures/chemistry/conditional_stereo_coordinates.json').read_text())
        molecule = Chem.AddHs(Chem.MolFromSmiles(data['source_smiles']))
        self.assertEqual(molecule.GetNumAtoms(), len(data['coordinates']))
        conformer = Chem.Conformer(molecule.GetNumAtoms())
        for index, point in enumerate(data['coordinates']):
            conformer.SetAtomPosition(index, point)
        molecule.AddConformer(conformer)
        return molecule

    def unique_completion(self, molecule):
        helper = getattr(structure_analysis, '_has_unique_exact_stereo_completion', None)
        self.assertIsNotNone(helper, 'Exact residual-completion criterion is absent.')
        return helper(molecule)

    def test_saved_typed_geometry_matches_the_independent_full_partition(self):
        molecule = self.fixture()
        before = molecule.ToBinary()
        with patch('checksi.analysis.structure_analysis.rdDistGeom.EmbedMolecule',
                   side_effect=AssertionError('Existing coordinates must not be regenerated.')):
            result = structure_analysis.carbon_partition_from_coordinates(molecule)
        self.assertTrue(result.complete, result.reason)
        self.assertEqual(partition(result.groups), partition(CASES['1.147']['groups']))
        self.assertEqual(molecule.ToBinary(), before)

    def test_genuine_unknown_RS_and_EZ_are_not_collapsed_by_mirror_equivalence(self):
        for smiles in ('CC(F)Cl', 'CC=CC'):
            with self.subTest(smiles=smiles):
                self.assertFalse(self.unique_completion(Chem.MolFromSmiles(smiles)))

    def test_incomplete_enumeration_or_enumeration_failure_never_discharge(self):
        self.assertFalse(self.unique_completion(Chem.MolFromSmiles('CC(O)C(F)C(Cl)C(Br)C')))
        with patch('checksi.analysis.structure_analysis.GetStereoisomerCount',
                   side_effect=RuntimeError('enumeration failure')):
            result = structure_analysis.carbon_partition_from_coordinates(self.fixture())
        self.assertFalse(result.complete)
        self.assertEqual(result.reason, 'coordinate_assessment_failed')

    def test_source_unknown_or_unsupported_representation_is_not_erased(self):
        source = Chem.MolFromSmiles('CC=CC')
        source.GetBondWithIdx(1).SetStereo(Chem.BondStereo.STEREOANY)
        infos = Chem.FindPotentialStereo(source)
        self.assertTrue(any(info.specified == Chem.StereoSpecified.Unknown for info in infos))
        for molecule in (source, Chem.MolFromSmiles('C[C@H](O)[C@H](O)C |&1:1,3|'),
                         Chem.MolFromSmiles('F[Pt@SP1](Cl)(Br)I')):
            molecule.AddConformer(Chem.Conformer(molecule.GetNumAtoms()))
            with self.subTest(smiles=Chem.MolToSmiles(molecule)):
                result = structure_analysis.carbon_partition_from_coordinates(molecule)
                self.assertFalse(result.complete)
                self.assertEqual(result.reason, 'unsupported_representation')


if __name__ == '__main__':
    unittest.main()
