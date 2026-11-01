"""Expert-bound carbon partitions; frame labels are independent of generated conformers."""

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from rdkit import Chem

from checksi.analysis.structure_analysis import analyze_molecular_structure_detailed
from checksi.analysis.stereochem import check_streo_desc
from checksi.config_schema import AppConfig
from checksi.errors import ErrorCode
from checksi.utils.error_handler import ErrorTracker


ORACLE_PATH = Path(__file__).parent / 'fixtures' / 'chemistry' / 'structure_oracle.json'
ORACLE = json.loads(ORACLE_PATH.read_text())
CASES = {case['id']: case for case in ORACLE['cases']}


def partition(groups):
    return frozenset(frozenset(group) for group in groups)


def complete_carbon_partition(molecule, result):
    """Expand the public representation's implicit singleton carbons for comparison."""
    groups = [[int(label[1:]) - 1 for label in group] for group in result.carbon_groups]
    covered = [index for group in groups for index in group]
    if len(covered) != len(set(covered)):
        raise AssertionError('The public groups duplicate an atom.')
    carbons = {atom.GetIdx() for atom in molecule.GetAtoms() if atom.GetSymbol() == 'C'}
    if not set(covered) <= carbons:
        raise AssertionError('The public carbon groups contain a noncarbon atom.')
    return groups + [[index] for index in sorted(carbons - set(covered))]


def frame_embedder(frame, new_to_old):
    """Supply hand coordinates at the geometry boundary, never infer expected groups."""
    def embed(molecule, parameters):
        conformer = Chem.Conformer(molecule.GetNumAtoms())
        coordinates = frame['heavy_coordinates']
        stereo_hydrogens = frame['stereo_hydrogen_coordinates']
        for atom in molecule.GetAtoms():
            index = atom.GetIdx()
            if atom.GetAtomicNum() != 1:
                point = coordinates[str(new_to_old[index])]
            else:
                parent = atom.GetNeighbors()[0].GetIdx()
                parent_origin = str(new_to_old[parent])
                if parent_origin in stereo_hydrogens:
                    point = stereo_hydrogens[parent_origin]
                else:
                    # These terminal H positions do not define a stereo centre.
                    point = tuple(value + offset for value, offset in zip(
                        coordinates[parent_origin], (0.1 * (index + 1), 0.5, 0.7),
                    ))
            conformer.SetAtomPosition(index, point)
        molecule.RemoveAllConformers()
        molecule.AddConformer(conformer)
        return 0
    return embed


def analyze_case(case, molecule=None, new_to_old=None):
    molecule = Chem.MolFromSmiles(case['smiles']) if molecule is None else molecule
    if 'frame' not in case:
        return analyze_molecular_structure_detailed(molecule)
    origins = list(range(molecule.GetNumAtoms())) if new_to_old is None else new_to_old
    frame = ORACLE['reference_frames'][case['frame']]
    with patch('checksi.analysis.structure_analysis.rdDistGeom.EmbedMolecule',
               side_effect=frame_embedder(frame, origins)):
        return analyze_molecular_structure_detailed(molecule)


class ExpertStructureOracleTests(unittest.TestCase):
    def test_hand_bound_maps_and_partitions_cover_every_carbon_once(self):
        for case in CASES.values():
            with self.subTest(case=case['id']):
                molecule = Chem.MolFromSmiles(case['smiles'])
                carbons = {atom.GetIdx() for atom in molecule.GetAtoms() if atom.GetSymbol() == 'C'}
                labelled = list(case['labels'].values())
                covered = [index for group in case['groups'] for index in group]
                self.assertEqual(set(labelled), carbons)
                self.assertEqual(len(labelled), len(carbons))
                self.assertEqual(set(covered), carbons)
                self.assertEqual(len(covered), len(carbons))
                self.assertEqual(len(case['groups']), case['count'])

    def test_full_counts_and_partitions_match_expert_oracle(self):
        for case in CASES.values():
            with self.subTest(case=case['id']):
                molecule = Chem.MolFromSmiles(case['smiles'])
                result = analyze_case(case, molecule)
                self.assertEqual(result.carbon_count, case['count'])
                self.assertEqual(partition(complete_carbon_partition(molecule, result)),
                                 partition(case['groups']))

    def test_original_atom_permutation_and_explicit_hydrogens_keep_partitions(self):
        for identifier in CASES:
            case = CASES[identifier]
            original = Chem.MolFromSmiles(case['smiles'])
            reversed_indices = list(reversed(range(original.GetNumAtoms())))
            variants = [
                (Chem.AddHs(original), list(range(original.GetNumAtoms()))),
                (Chem.RenumberAtoms(original, reversed_indices), reversed_indices),
            ]
            for molecule, new_to_old in variants:
                with self.subTest(case=identifier, atoms=molecule.GetNumAtoms()):
                    result = analyze_case(case, molecule, new_to_old)
                    actual = [[new_to_old[index] for index in group]
                              for group in complete_carbon_partition(molecule, result)]
                    self.assertEqual(result.carbon_count, case['count'])
                    self.assertEqual(partition(actual), partition(case['groups']))

    def test_catalogue_142_four_configurations_keep_exact_groups(self):
        case = CASES['1.142']
        for tags in case['admissible_tetrahedral_tags']:
            molecule = Chem.MolFromSmiles(case['smiles'])
            for index, orientation in tags.items():
                molecule.GetAtomWithIdx(int(index)).SetChiralTag(
                    getattr(Chem.ChiralType, 'CHI_TETRAHEDRAL_' + orientation))
            with self.subTest(tags=tags):
                result = analyze_case(case, molecule)
                self.assertEqual(result.carbon_count, case['count'])
                self.assertEqual(partition(complete_carbon_partition(molecule, result)),
                                 partition(case['groups']))

    def test_alpha_pinene_real_bridge_configurations_keep_ten_singletons(self):
        case = CASES['alpha_pinene']
        for tags in case['admissible_tetrahedral_tags']:
            molecule = Chem.MolFromSmiles(case['smiles'])
            for index, orientation in tags.items():
                molecule.GetAtomWithIdx(int(index)).SetChiralTag(
                    getattr(Chem.ChiralType, 'CHI_TETRAHEDRAL_' + orientation))
            with self.subTest(tags=tags):
                result = analyze_case(case, molecule)
                self.assertEqual(result.carbon_count, 10)
                self.assertEqual(partition(complete_carbon_partition(molecule, result)),
                                 partition(case['groups']))

    def test_direct_cb_site_is_the_actual_omission_environment(self):
        case = CASES['1.147']
        molecule = Chem.MolFromSmiles(case['smiles'])
        self.assertEqual(case['direct_CB_indices'], [5])
        self.assertTrue(any(atom.GetSymbol() == 'B'
                            for atom in molecule.GetAtomWithIdx(5).GetNeighbors()))
        self.assertIn([5], case['groups'])
        self.assertEqual(case['count'] - case['accepted_observation_count'], 1)

    def test_prochiral_native_counts_do_not_require_remote_activation(self):
        for identifier in ('prochiral_OH', 'prochiral_OEt', 'prochiral_OAc', 'prochiral_Oallyl'):
            case = CASES[identifier]
            original = Chem.MolFromSmiles(case['smiles'])
            for molecule in (original, Chem.AddHs(original), Chem.RenumberAtoms(
                    original, list(reversed(range(original.GetNumAtoms()))))):
                with self.subTest(case=identifier, atoms=molecule.GetNumAtoms()):
                    self.assertEqual(analyze_molecular_structure_detailed(molecule).carbon_count,
                                     case['count'])

    def test_lower_coincidence_options_do_not_supply_merged_equivalence_groups(self):
        for identifier in ('diisopropyl_L_tartrate', 'S_isopropyl_methyl_sulfoxide',
                           'L_valine', '1.142', '1.145', '1.147', 'alpha_pinene'):
            case = CASES[identifier]
            with self.subTest(case=identifier):
                result = analyze_case(case)
                lower = [option for option in result.c13_alternatives
                         if option.count == case['manual_count']]
                self.assertTrue(lower)
                self.assertTrue(all(option.manual_check_only for option in lower))
                self.assertTrue(all(option.groups_dp5 == [] for option in lower))
                self.assertEqual(result.carbon_count, case['count'])

    def test_relevant_stereo_inventory_includes_supported_heteroatoms_and_ring_sites(self):
        for case in ORACLE['stereo_inventory_cases']:
            with self.subTest(case=case['id']):
                molecule = Chem.MolFromSmiles(case['smiles'])
                # This independently exercises the pinned API detection contract.
                # Expected types/indices were hand-entered before this first call.
                actual_elements = []
                for info in Chem.FindPotentialStereo(Chem.Mol(molecule)):
                    element = (molecule.GetAtomWithIdx(info.centeredOn).GetSymbol()
                               if info.type == Chem.StereoType.Atom_Tetrahedral else None)
                    actual_elements.append((str(info.type), info.centeredOn,
                                            element, str(info.specified)))
                expected_elements = [(item['type'], item['centered_on'], item['element'],
                                      item['specified']) for item in case['expected_elements']]
                self.assertEqual(sorted(actual_elements), sorted(expected_elements))
                result = analyze_molecular_structure_detailed(molecule)
                self.assertEqual(result.stereocenter_count,
                                 case['expected_relevant_center_count'])

    def test_structural_meso_status_is_explicit_complete_and_representation_aware(self):
        for case in ORACLE['meso_cases']:
            with self.subTest(case=case['id']):
                result = analyze_molecular_structure_detailed(Chem.MolFromSmiles(case['smiles']))
                assessment = getattr(result, 'stereo_assessment', None)
                self.assertIsNotNone(assessment, 'Structural meso assessment is absent.')
                self.assertEqual(assessment.status, case['expected_status'])
                self.assertEqual(assessment.enumeration_complete,
                                 case['expected_enumeration_complete'])
                if 'expected_reason' in case:
                    self.assertEqual(assessment.reason, case['expected_reason'])
                if 'expected_distinct_isomers' in case:
                    self.assertEqual(assessment.distinct_isomer_count,
                                     case['expected_distinct_isomers'])

    def test_impossible_alpha_pinene_bridges_are_explicitly_refused_or_excluded(self):
        case = CASES['alpha_pinene']
        for tags in case['inadmissible_tetrahedral_tags']:
            molecule = Chem.MolFromSmiles(case['smiles'])
            for index, orientation in tags.items():
                molecule.GetAtomWithIdx(int(index)).SetChiralTag(
                    getattr(Chem.ChiralType, 'CHI_TETRAHEDRAL_' + orientation))
            with self.subTest(tags=tags):
                result = analyze_molecular_structure_detailed(molecule)
                assessment = getattr(result, 'stereo_assessment', None)
                self.assertIsNotNone(assessment, 'Physical stereo disposition is absent.')
                self.assertEqual(assessment.configuration_status, 'inadmissible')
                self.assertEqual(assessment.status, 'unassessed')

    def test_ring_cis_trans_inventory_does_not_demand_atom_RS_descriptors(self):
        cfg = AppConfig.model_validate({
            'paths': {'input_path': 'input.pdf', 'output_dir': '.'},
            'modes': {'deep_name_analysis': 'on', 'output_mode': 'essential'},
            'solvents': {'entries': []},
            'errors': {'default_severity': 'disabled', 'overrides': {
                ErrorCode.STEREO_DESCRIPTOR_INVALID: 'warning',
            }},
        })
        output = check_streo_desc(
            cfg, ErrorTracker(), 'cis-1,4-dimethylcyclohexane', 100, 0, 2,
            'C[C@H]1CC[C@@H](C)CC1',
        )
        self.assertEqual(output, [], 'Ring relative stereochemistry does not require atom R/S names.')

    def test_stereodescriptor_assessment_does_not_depend_on_spectral_fraction(self):
        cfg = AppConfig.model_validate({
            'paths': {'input_path': 'input.pdf', 'output_dir': '.'},
            'modes': {'deep_name_analysis': 'on', 'output_mode': 'essential'},
            'solvents': {'entries': []},
            'errors': {'default_severity': 'disabled', 'overrides': {
                ErrorCode.STEREO_CHECK_REQUIRED: 'warning',
                ErrorCode.STEREO_DESCRIPTOR_INVALID: 'warning',
            }},
        })
        for name in ('(2R,3S)-example', '(2RS,3SR)-example'):
            outputs = []
            for fraction in (0.1, 0.45, 0.8, 100):
                outputs.append(check_streo_desc(
                    cfg, ErrorTracker(), name, fraction, 0, 2,
                    'O=C(O)[C@@H](O)[C@H](O)C(=O)O',
                ))
            with self.subTest(name=name):
                self.assertTrue(all(value == outputs[0] for value in outputs[1:]), outputs)


if __name__ == '__main__':
    unittest.main()
