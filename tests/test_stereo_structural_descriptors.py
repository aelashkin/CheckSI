"""Descriptor decisions consume structure-derived status and appropriate atom roles."""

import json
import unittest
from unittest.mock import patch

from rdkit import Chem

from checksi.analysis.stereochem import _check_streo_desc
from checksi.analysis.structure_analysis import assess_molecular_stereochemistry
from checksi.config_schema import AppConfig
from checksi.controller import CheckSIController
from checksi.errors import ErrorCode
from checksi.errors.findings import Finding, messages
from checksi.utils.error_handler import ErrorTracker


def config(severity='warning'):
    return AppConfig.model_validate({
        'paths': {'input_path': 'input.pdf', 'output_dir': '.'},
        'modes': {'deep_name_analysis': 'on', 'output_mode': 'essential'},
        'solvents': {'entries': []},
        'errors': {'default_severity': 'disabled', 'overrides': {
            ErrorCode.STEREO_CHECK_REQUIRED: severity,
            ErrorCode.STEREO_DESCRIPTOR_INVALID: severity,
        }},
    })


def assess(smiles):
    return assess_molecular_stereochemistry(Chem.MolFromSmiles(smiles))


class StructuralDescriptorTests(unittest.TestCase):
    def typed(self, name, smiles, fraction=0.45, severity='warning'):
        return _check_streo_desc(config(severity), ErrorTracker(), name, fraction, 0, 0,
                                 smiles, stereo_assessment=assess(smiles))

    def test_status_messages_never_depend_on_spectral_fraction(self):
        for smiles, variant in (
                ('OC[C@H](O)[C@H](O)[C@H](O)CO', 'structural_meso'),
                ('CC(C)OC(=O)[C@H](O)[C@@H](O)C(=O)OC(C)C', 'structural_non_meso'),
                ('CC(O)C(O)C', 'meso_possible')):
            expected = None
            for fraction in (0.1, 0.45, 0.8, 100, float('nan')):
                diagnostics = self.typed('(1R,2S)-example', smiles, fraction)
                findings = [d for d in diagnostics if isinstance(d, Finding)]
                self.assertEqual([d.variant for d in findings], [variant])
                self.assertEqual(findings[0].context['meso_status'], assess(smiles).status)
                self.assertNotIn('eq_c_fraction', findings[0].context)
                self.assertNotIn('Fraction of equivalent', ' '.join(messages(diagnostics)))
                expected = messages(diagnostics) if expected is None else expected
                self.assertEqual(messages(diagnostics), expected)

    def test_heteroatom_inventory_controls_too_few_and_omission_paths(self):
        for smiles in ('C[S@](=O)C(C)C', 'C[P@](=O)(c1ccccc1)C(C)C',
                       'C[N@+](CC)(CCC)CCCC'):
            with self.subTest(smiles=smiles):
                findings = [d for d in self.typed('(1RS,2SR)-example', smiles) if isinstance(d, Finding)]
                self.assertEqual([d.variant for d in findings], ['too_few_stereocenters'])
                self.assertEqual(findings[0].context['stereocenter_count'], 1)
                findings = [d for d in self.typed('(RS)-example', smiles) if isinstance(d, Finding)]
                self.assertEqual([d.variant for d in findings], ['can_be_omitted'])

    def test_missing_atom_descriptors_distinguish_relative_ring_and_incomplete_inventory(self):
        for smiles in ('OC[C@H](O)[C@H](O)[C@H](O)CO', 'CC(O)C(O)C'):
            findings = [d for d in self.typed('example', smiles) if isinstance(d, Finding)]
            self.assertEqual([d.variant for d in findings], ['missing_descriptors'])
            self.assertEqual(findings[0].context['stereocenter_count'], 2)
        for name, smiles in (('cis-1,4-dimethylcyclohexane', 'C[C@H]1CC[C@@H](C)CC1'),
                             ('example', 'F[Pt@SP1](Cl)(Br)I'),
                             ('example', 'CC(O)C(F)C(Cl)C(Br)C')):
            diagnostics = self.typed(name, smiles)
            self.assertFalse(any(isinstance(d, Finding) for d in diagnostics))

    def test_single_centre_RS_SR_deduplicates_before_omission(self):
        findings = [d for d in self.typed('(RS,SR)-example', 'C[S@](=O)C(C)C') if isinstance(d, Finding)]
        self.assertEqual([d.variant for d in findings], ['invalid_type'])

    def test_separate_valid_group_keeps_its_own_descriptor_finding(self):
        multicentre = 'Cc1ccc(NC[C@H]2CC[C@@H](CNc3ccc(C)cc3)C2)cc1'
        cases = [('(RS,SR)-x-(RS)-y', 'CC(O)C(=O)O', 'can_be_omitted')]
        cases += [(name, multicentre, 'descriptor_type_incorrect') for name in (
            '(RS,SR)-x-(RS)-y', '(R,S)-x-(R)-y', '(R*,S*)-x-(R*)-y')]
        for name, smiles, expected in cases:
            for severity in ('disabled', 'info', 'warning', 'critical'):
                with self.subTest(name=name, smiles=smiles, severity=severity):
                    tracker = ErrorTracker()
                    diagnostics = _check_streo_desc(config(severity), tracker, name, 100, 0, 0,
                        smiles, stereo_assessment=assess(smiles))
                    findings = [d for d in diagnostics if isinstance(d, Finding)
                                and d.code == ErrorCode.STEREO_DESCRIPTOR_INVALID]
                    self.assertEqual([d.variant for d in findings],
                                     [] if severity == 'disabled' else ['invalid_type', expected])
                    self.assertEqual([d.variant for d in tracker.occurrences
                                      if d.code == ErrorCode.STEREO_DESCRIPTOR_INVALID],
                                     ['invalid_type', expected])

    def test_confirmed_meso_is_not_treated_as_a_racemate_or_only_possible_form(self):
        findings = [d for d in self.typed('(1RS,2SR)-example', 'OC[C@H](O)[C@H](O)[C@H](O)CO')
                    if isinstance(d, Finding)]
        self.assertEqual([d.variant for d in findings], ['structural_meso_descriptor'])
        findings = [d for d in self.typed('(1RS,2SR)-example', 'CC(O)C(O)C') if isinstance(d, Finding)]
        self.assertFalse(findings)

    def test_controller_propagates_the_same_structure_result_with_overlap_or_visibility_changes(self):
        smiles = 'OC[C@H](O)[C@H](O)[C@H](O)CO'
        description = ('1H NMR (400 MHz, CDCl3): δ 3.50 (s, 12H). '
                       '13C NMR (100 MHz, CDCl3): δ 70.0, 60.0, 50.0.')
        for severity in ('warning', 'disabled'):
            outputs = []
            for carbon in ('70.0, 60.0, 50.0', '70.0 (2C), 60.0, 50.0'):
                with patch('checksi.controller._smiles_and_name_from_paragraph',
                           return_value=(smiles, '(1R,2S)-example', '1', {}, [])):
                    result = CheckSIController(config(severity)).run_analysis(
                        ['Name (1)', description.replace('70.0, 60.0, 50.0', carbon)])
                compound, = result['compounds']
                self.assertEqual(compound['stereo_assessment']['status'], 'confirmed_meso')
                self.assertEqual(json.loads(json.dumps(compound['stereo_assessment'], allow_nan=False)),
                                 compound['stereo_assessment'])
                outputs.append([line for line in compound['all_warnings'] if 'stereo_check_required.' in line])
            self.assertEqual(outputs[0], outputs[1])


if __name__ == '__main__':
    unittest.main()
