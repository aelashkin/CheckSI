"""Nucleus notes require their own substitution proof; counts are conditional."""

import unittest
from unittest.mock import patch

from rdkit import Chem

from checksi.analysis.nmr import check_nmr
from checksi.analysis.structure_analysis import analyze_molecular_structure_detailed
from checksi.config import ConfigManager
from checksi.config_schema import ErrorSeverity
from checksi.controller import CheckSIController
from checksi.dp5_enrichment import DP5PreparationSkip, _compound_request
from checksi.errors.tracker import ErrorTracker


class NucleusConfigurationTests(unittest.TestCase):
    def analyze(self, smiles, text, *, disabled=()):
        config = ConfigManager.load_factory()
        for code in disabled:
            config.errors.overrides[code] = ErrorSeverity.DISABLED
        tracker = ErrorTracker()
        return CheckSIController(config)._analyze_single_description(text, smiles, tracker), tracker

    def test_unique_nucleus_with_unrelated_carbon_stereo_never_gets_pair_note(self):
        for nucleus, smiles in (
            ('19F', 'FCCC[C@H](O)C(C)C'),
            ('29Si', 'C[Si](C)(C)CC[C@H](O)C(C)C'),
            ('11B', 'OB(O)CC[C@H](O)C(C)C'),
            ('31P', 'OP(=O)(O)CC[C@H](O)C(C)C'),
        ):
            with self.subTest(nucleus=nucleus):
                _, tracker = self.analyze(smiles, f'{nucleus} NMR (100 MHz, CDCl3): δ 20.0, 10.0.')
                self.assertEqual(tracker.get_count('nmr_signal_count_structure_mismatch'), 1)
                self.assertEqual(tracker.get_count('nmr_diastereotopic_note'), 0)

    def test_each_nucleus_proof_supports_pair_note_with_or_without_initial_stereo(self):
        for nucleus, pair in (
            ('19F', 'F'), ('29Si', '[Si](C)(C)C'),
            ('11B', 'B(O)O'), ('31P', 'P(=O)(O)O'),
        ):
            for smiles in (f'C[C@H](O)C({pair}){pair}', f'OC(C({pair}){pair})C({pair}){pair}'):
                with self.subTest(nucleus=nucleus, smiles=smiles):
                    result, tracker = self.analyze(smiles, f'{nucleus} NMR (100 MHz, CDCl3): δ 20.0.')
                    self.assertEqual(tracker.get_count('nmr_diastereotopic_note'), 1)
                    self.assertTrue(any(nucleus in note and 'substitution' in note for note in result['warnings']))

    def test_failed_substitution_cannot_fall_back_to_global_pair_guess(self):
        with patch('checksi.analysis.structure_analysis._isomer_substitution_keys', return_value=None):
            _, tracker = self.analyze('C[C@H](O)C(F)F', '19F NMR (100 MHz, CDCl3): δ 20.0.')
        self.assertEqual(tracker.get_count('nmr_diastereotopic_note'), 0)

    def test_configuration_counts_are_warning_and_select_a_compatible_isomer(self):
        for count in (4, 6):
            with self.subTest(count=count):
                signals = ', '.join(f'{100-10*i}.0' for i in range(count))
                result, tracker = self.analyze('CC(O)C(C)C(C)O', f'13C NMR (100 MHz, CDCl3): δ {signals}.')
                self.assertTrue(result['run_dp5'], result['dp5_refusal_details'])
                self.assertEqual(result['carbon_scope']['expected_counts'], [4, 6])
                self.assertEqual(result['carbon_scope']['full_environment_count'], count)
                self.assertTrue(result['carbon_scope']['configuration_dependent'])
                self.assertEqual(result['stereo_assessment']['status'], 'meso_possible')
                self.assertEqual(tracker.get_count('c13_count_structure_check'), 0)
                conditional, = [item for item in tracker.occurrences
                                if item.code.value == 'nmr_configuration_dependent']
                self.assertEqual(conditional.severity, ErrorSeverity.WARNING)
                self.assertIn('4 or 6', ' '.join(result['warnings']))

    def test_hidden_configuration_warning_never_changes_selection(self):
        text = '13C NMR (100 MHz, CDCl3): δ 100.0, 90.0, 80.0, 70.0.'
        shown, _ = self.analyze('CC(O)C(C)C(C)O', text)
        hidden, tracker = self.analyze('CC(O)C(C)C(C)O', text, disabled=('nmr_configuration_dependent',))
        self.assertEqual(shown['dp5_carbon_groups'], hidden['dp5_carbon_groups'])
        self.assertEqual(shown['run_dp5'], hidden['run_dp5'])
        self.assertEqual(tracker.get_count('nmr_configuration_dependent'), 0)

    def test_fluorine_only_variation_does_not_claim_carbon_count_variation(self):
        for smiles, count_text in (('FC(F)C(O)C(O)C(F)F', '2 environments'),
                                   ('CC(O)C(F)(F)C(O)C', '1 or 2')):
            with self.subTest(smiles=smiles):
                result, tracker = self.analyze(smiles,
                    '19F NMR (100 MHz, CDCl3): δ 20.0, 10.0. '
                    '13C NMR (100 MHz, CDCl3): δ 100.0, 90.0, 80.0.')
                conditional, = [item for item in tracker.occurrences
                                if item.code.value == 'nmr_configuration_dependent']
                self.assertEqual(conditional.context['nuclei'], '19F')
                self.assertIn(count_text, ' '.join(result['warnings']))
                self.assertNotIn('2 or 2', ' '.join(result['warnings']))
                self.assertFalse(result['carbon_scope']['configuration_dependent'])
                self.assertTrue(result['carbon_scope']['isomer_scope_dependent'])

    def test_known_scoring_domain_refusal_keeps_configuration_counts_without_rethrow(self):
        source = analyze_molecular_structure_detailed(Chem.MolFromSmiles('CC(O)C(C)C(C)O'))
        tracker = ErrorTracker()
        refusal, scope = {}, {}
        with patch('checksi.analysis.nmr.derive_observation_policy',
                   side_effect=AssertionError('known bounded refusal must not be retried')):
            result = check_nmr(ConfigManager.load_factory(), tracker,
                '13C NMR (100 MHz, CDCl3): δ 100.0, 90.0, 80.0, 70.0.', '13C', 1,
                source.carbon_count, 400, source.stereocenter_count, source.one_pair_center_count,
                None, 0, 0, carbon_groups=source.carbon_groups, structure_result=source,
                policy_reason='atom_limit', dp5_refusal_details=refusal, carbon_scope_out=scope)
        self.assertFalse(result[6])
        self.assertEqual(refusal['cause'], 'atom_limit')
        self.assertEqual(scope['expected_counts'], [4, 6])
        self.assertEqual(tracker.get_count('nmr_configuration_dependent'), 1)
        self.assertEqual(tracker.get_count('c13_count_structure_check'), 0)

    def test_group_limit_is_retried_for_each_supported_partition(self):
        # Scale the existing cap to straddle this independently proved 6/4 pair;
        # no oversized hypothetical molecule or relaxed production limit is used.
        with patch('checksi.dp5q.assignment.MAX_GROUPS', 5):
            result, tracker = self.analyze('CC(O)C(C)C(C)O',
                '13C NMR (100 MHz, CDCl3): δ 100.0, 90.0, 80.0, 70.0.')
        self.assertTrue(result['run_dp5'], result['dp5_refusal_details'])
        self.assertEqual(result['carbon_scope']['full_environment_count'], 4)
        self.assertEqual(result['carbon_scope']['expected_counts'], [4, 6])
        self.assertEqual(tracker.get_count('c13_count_structure_check'), 0)

    def test_conditional_boron_absence_is_unassessed_and_never_scored(self):
        result, tracker = self.analyze('CC(O)C(B(O)O)C(C)O',
                                      '13C NMR (100 MHz, CDCl3): δ 100.0, 90.0.')
        self.assertEqual(result['carbon_scope']['expected_counts'], [3, 5])
        self.assertFalse(result['run_dp5'])
        self.assertEqual(result['carbon_scope']['partition_reason'], 'configuration_dependent_partition')
        self.assertEqual(result['carbon_scope']['accepted_missing_direct_cb_count'], 0)
        self.assertEqual(tracker.get_count('c13_boron_signal_not_observed'), 0)
        self.assertEqual(tracker.get_count('c13_count_structure_check'), 0)
        self.assertEqual(tracker.get_count('nmr_configuration_dependent'), 1)
        with self.assertRaises(DP5PreparationSkip) as skipped:
            _compound_request(ConfigManager.load_factory(), {'smiles': 'CC(O)C(B(O)O)C(C)O', **result}, 1)
        self.assertEqual(skipped.exception.reason, 'boron_not_supported')

    def test_conditional_boron_salt_component_cannot_certify_missing_sites(self):
        for smiles in ('CC(O)C(B(O)O)C(C)O.[Na+]',
                       '[Na+].CC(O)C([B-](F)(F)F)C(C)O'):
            with self.subTest(smiles=smiles):
                result, tracker = self.analyze(smiles, '13C NMR (100 MHz, CDCl3): δ 100.0, 90.0.')
                self.assertFalse(result['run_dp5'])
                self.assertEqual(result['carbon_scope']['partition_reason'], 'configuration_dependent_partition')
                self.assertEqual(result['carbon_scope']['accepted_missing_direct_cb_count'], 0)
                self.assertEqual(tracker.get_count('c13_boron_signal_not_observed'), 0)
                self.assertEqual(tracker.get_count('c13_count_structure_check'), 0)


if __name__ == '__main__':
    unittest.main()
