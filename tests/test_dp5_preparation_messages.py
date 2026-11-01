"""Preparation refusals preserve their cause without becoming calculation failures."""

import unittest
from unittest.mock import patch

from checksi.config import ConfigManager
from checksi.config_schema import DeepNameAnalysis
from checksi.controller import CheckSIController
from checksi.dp5_enrichment import enrich_analysis_with_dp5
from checksi.dp5q.assignment import AssignmentError
from checksi.dp5q.contracts import CheckResult


class PreparationMessageTests(unittest.TestCase):
    def prepare(self, *, reason=None, shifts=None, details=None):
        config = ConfigManager.load_factory()
        config.dp5.enabled = True
        compound = {'id': 1, 'smiles': 'CCO', 'run_dp5': reason is None,
                    'dp5_carbon_groups': [], 'dp5_c_signals_list': shifts or [60., 20.],
                    'dp5_refusal_details': {'cause': reason, **(details or {})} if reason else {}}
        result = enrich_analysis_with_dp5(config, {'compounds': [compound]})
        return compound, result

    def test_structural_refusal_is_not_an_analysis_failure(self):
        compound, result = self.prepare(reason='structure_partition_incomplete',
            details={'structure_reason': 'substitution_incomplete'})
        self.assertEqual(compound['dp5']['reason'], 'structure_partition_incomplete')
        self.assertEqual(result['error_stats']['dp5_analysis_failed'], 0)
        self.assertEqual(result['error_stats']['dp5_not_applicable'], 1)
        self.assertEqual(compound['all_warnings'], [
            'dp5_not_applicable. DP5q not applicable: structure_partition_incomplete: substitution_incomplete.'])

    def test_stale_structure_reason_cannot_replace_the_active_parser_reason(self):
        for reason, explanation in (
            ('malformed_separators', 'the 13C signal separators are malformed'),
            ('invalid_carbon_annotation', 'a carbon atom annotation is invalid'),
        ):
            with self.subTest(reason=reason):
                compound, _ = self.prepare(reason=reason, details={'structure_reason': 'unsupported_representation'})
                text = ' '.join(compound['all_warnings'])
                self.assertIn(f'{reason}: {explanation}', text)
                self.assertNotIn('unsupported_representation', text)
                self.assertNotIn('analysis failed', text)

    def test_policy_exception_message_survives_preparation(self):
        reason = 'unsupported_coincidence_policy'
        message = 'Structural coincidence coalitions overlap.'
        compound, _ = self.prepare(reason=reason, details={'reason_details': {reason: message}})
        self.assertIn(f'{reason}: {message}', ' '.join(compound['all_warnings']))
        self.assertNotIn('13C validation did not accept', ' '.join(compound['all_warnings']))

    def test_controller_keeps_the_policy_exception_message_for_the_adapter(self):
        config = ConfigManager.load_factory()
        config.dp5.enabled = True
        config.modes.deep_name_analysis = DeepNameAnalysis.ON
        reason, message = 'unsupported_coincidence_policy', 'Structural coincidence coalitions overlap.'
        with patch('checksi.controller._smiles_and_name_from_paragraph',
                   return_value=('CCO', 'ethanol', '1', {}, [])), patch(
                'checksi.controller.derive_observation_policy', side_effect=AssignmentError(reason, message)):
            data = CheckSIController(config).run_analysis([
                'ethanol (1)', '1H NMR (400 MHz, CDCl3): δ 1.20 (s, 3H). '
                '13C NMR (100 MHz, CDCl3): δ 60.0, 20.0.'])
        compound = data['compounds'][0]
        self.assertEqual(compound['dp5_refusal_details']['reason_details'], {reason: message})
        enriched = enrich_analysis_with_dp5(config, data)
        self.assertEqual(enriched['dp5_summary']['skipped'], 1)
        self.assertIn(f'{reason}: {message}', ' '.join(compound['all_warnings']))

    def test_nonfinite_input_with_matching_count_has_a_numeric_reason(self):
        compound, result = self.prepare(shifts=[float('nan'), 20.])
        self.assertEqual(compound['dp5']['reason'], 'invalid_request')
        self.assertIn('finite numeric', ' '.join(compound['all_warnings']))
        self.assertNotIn('coverage', ' '.join(compound['all_warnings']))
        self.assertEqual(result['dp5_summary']['failed'], 0)

    def test_unverified_success_keeps_the_complete_expert_warning_sentence(self):
        config = ConfigManager.load_factory()
        config.dp5.enabled = True
        compound = {'id': 1, 'smiles': 'CCO', 'run_dp5': True,
                    'dp5_carbon_groups': [], 'dp5_c_signals_list': [60., 20.]}
        scored = CheckResult(compound_id='1', status='assessed', score_raw=.5,
            provenance={'selected_isomer_smiles': 'CCO', 'stereo_verification_complete': False,
                        'coordinate_partition_verified': False})
        with patch('checksi.dp5_enrichment.DP5qSession') as session:
            session.return_value.__enter__.return_value.check.return_value = scored
            result = enrich_analysis_with_dp5(config, {'compounds': [compound]})
        self.assertEqual(result['dp5_summary']['successful'], 1)
        self.assertEqual(compound['dp5']['score_raw'], .5)
        self.assertEqual(compound['all_warnings'], [
            '❗ dp5_configuration_dependent. DP5q calculated for one stereoisomer only; '
            'another isomer may give a very different result. '
            'Coordinate verification of stereochemistry or carbon equivalence is incomplete. '
            'Selected isomer: CCO.'])


if __name__ == '__main__':
    unittest.main()
