"""Hand-bound graph controls; no new experimental or scoring gold is claimed."""

import json
import unittest

from checksi.config import ConfigManager
from checksi.config_schema import ErrorSeverity
from checksi.controller import CheckSIController
from checksi.dp5_enrichment import DP5PreparationSkip, _compound_request
from checksi.errors.tracker import ErrorTracker


class BoronCarbonDiagnosticTests(unittest.TestCase):
    def analyze(self, smiles, count, *, disabled=False, signals=None):
        cfg = ConfigManager.load_factory()
        if disabled:
            cfg.errors.overrides['c13_boron_signal_not_observed'] = ErrorSeverity.DISABLED
        text = '13C NMR (100 MHz, CDCl3): δ ' + (signals or ', '.join(f'{180-10*i}.0' for i in range(count))) + '.'
        tracker = ErrorTracker()
        result = CheckSIController(cfg)._analyze_single_description(text, smiles, tracker)
        json.dumps(result, allow_nan=False)
        return result, tracker

    def test_accepted_absence_does_not_claim_equivalent_carbons(self):
        for smiles in ('CCB(O)O', 'CC[B-](F)(F)F.[K+]'):
            for disabled in (False, True):
                with self.subTest(smiles=smiles, disabled=disabled):
                    result, tracker = self.analyze(smiles, 1, disabled=disabled)
                    scope = result['carbon_scope']
                    self.assertEqual((scope['full_environment_count'], scope['observation_count'],
                                      scope['represented_signal_count'], scope['accepted_missing_direct_cb_count']),
                                     (2, 1, 1, 1))
                    self.assertEqual(result['eq_c_ratio'], 0.5)
                    self.assertEqual(result['dp5_c_signals_list'], [180.0])
                    self.assertFalse(result['run_dp5'])
                    self.assertEqual(tracker.get_count('c13_count_structure_check'), 0)
                    self.assertEqual(tracker.get_count('c13_boron_signal_not_observed'), 0 if disabled else 1)
                    occurrence, = [item for item in tracker.occurrences
                                   if item.code.value == 'c13_boron_signal_not_observed']
                    self.assertEqual(occurrence.severity,
                                     ErrorSeverity.DISABLED if disabled else ErrorSeverity.INFO)
                    with self.assertRaises(DP5PreparationSkip) as skipped:
                        _compound_request(ConfigManager.load_factory(), {'smiles': smiles, **result}, 1)
                    self.assertEqual(skipped.exception.reason, 'boron_not_supported')
                    self.assertFalse(any('equivalent C in 13C NMR:' in note for note in result['warnings']))

    def test_measured_boron_signals_keep_the_ordinary_equivalent_caption(self):
        for smiles, count, ratio, caption in (
                ('CCB(O)O', 2, 1.0, 'equivalent C in 13C NMR: 2 - 2 = 0 '),
                ('CCB1OC(C)(C)C(C)(C)O1', 4, 0.5, 'equivalent C in 13C NMR: 8 - 4 = 4 ')):
            with self.subTest(smiles=smiles):
                result, tracker = self.analyze(smiles, count)
                scope = result['carbon_scope']
                self.assertEqual(scope['accepted_missing_direct_cb_count'], 0)
                self.assertEqual((scope['full_environment_count'], scope['observation_count']), (count, count))
                self.assertEqual(result['eq_c_ratio'], ratio)
                self.assertEqual(tracker.get_count('c13_boron_signal_not_observed'), 0)
                self.assertEqual(tracker.get_count('c13_count_structure_check'), 0)
                self.assertFalse(result['run_dp5'])
                self.assertIn(caption, result['warnings'])

    def test_intact_counterion_retains_original_indices_and_accepts_one_direct_absence(self):
        for smiles, direct, fragments in [('CC[B-](F)(F)F.[K+]', [1, 2], [[0,1,2,3,4,5],[6]]),
                                         ('[K+].CC[B-](F)(F)F', [2, 3], [[0],[1,2,3,4,5,6]])]:
            with self.subTest(smiles=smiles):
                result, tracker = self.analyze(smiles, 1)
                scope = result['carbon_scope']
                self.assertEqual(tracker.get_count('c13_count_structure_check'), 0)
                self.assertEqual(tracker.get_count('c13_boron_signal_not_observed'), 1)
                self.assertEqual((scope['full_environment_count'], scope['observation_count'],
                                  scope['represented_signal_count'], scope['accepted_missing_direct_cb_count']), (2,1,1,1))
                self.assertEqual(scope['direct_cb_atom_pairs'], [direct])
                self.assertEqual(scope['source_fragments'], fragments)
                self.assertEqual(scope['accepted_missing_direct_cb_environments'], [[f'C{direct[0]+1}']])
                self.assertTrue(scope['partition_complete'])
                self.assertFalse(scope['scoring_partition_complete'])
                self.assertFalse(result['run_dp5'])

    def test_named_families_full_measured_remote_and_symmetric_deficits(self):
        # Expected full/direct counts were frozen by hand before parser probes.
        for smiles, full, direct in [('CCB(O)O', 2, 1), ('CCB1OC(C)(C)C(C)(C)O1', 4, 1),
                ('C[B-]12OC(=O)C[N+]1(C)CC(=O)O2', 4, 1),
                ('CCB(C)CCC', 6, 3), ('CCB(CC)CC', 2, 1), ('CCOB(O)O', 2, 0)]:
            for missing in (0, 1):
                with self.subTest(smiles=smiles, missing=missing):
                    result, tracker = self.analyze(smiles, full-missing)
                    accepted = missing if missing <= direct else 0
                    self.assertEqual(result['carbon_scope']['full_environment_count'], full)
                    self.assertEqual(result['carbon_scope']['accepted_missing_direct_cb_count'], accepted)
                    self.assertEqual(tracker.get_count('c13_boron_signal_not_observed'), int(bool(accepted)))
                    self.assertEqual(tracker.get_count('c13_count_structure_check'), int(bool(missing and not accepted)))
                    self.assertFalse(result['run_dp5'])

    def test_exact_147_full18_observed17_preserves_count_and_independent_notes(self):
        smiles = 'CC1(C)OB(C(=C2CCCC2)c2ccc3ccccc3c2)OC1(C)C'
        signals = ('145.63, 141.22, 133.64, 131.83, 128.16, 127.88, 127.66, 127.15, '
                   '126.92, 125.58, 125.02, 83.16, 34.26, 34.00, 26.65, 26.21, 24.95')
        for disabled in (False, True):
            result, tracker = self.analyze(smiles, 17, disabled=disabled, signals=signals)
            scope = result['carbon_scope']
            self.assertEqual((scope['full_environment_count'], scope['observation_count'],
                              scope['represented_signal_count'], scope['accepted_missing_direct_cb_count']), (18,17,17,1))
            self.assertEqual(scope['accepted_missing_direct_cb_environments'], [['C6']])
            self.assertEqual(scope['direct_cb_atom_pairs'], [[5,4]])
            self.assertEqual(tracker.get_count('c13_count_structure_check'), 0)
            self.assertEqual(tracker.get_count('c13_boron_signal_not_observed'), 0 if disabled else 1)
            self.assertGreater(tracker.get_count('nmr_signal_rounding_needed'), 0)
            self.assertFalse(result['run_dp5'])

    def test_missing_more_than_unique_direct_environments_remains_a_low_count(self):
        result, tracker = self.analyze('CCB1OC(C)(C)C(C)(C)O1', 2)
        self.assertEqual(result['carbon_scope']['accepted_missing_direct_cb_count'], 0)
        self.assertEqual(tracker.get_count('c13_count_structure_check'), 1)
        self.assertEqual(tracker.get_count('c13_boron_signal_not_observed'), 0)

    def test_valid_explicit_annotations_share_the_direct_absence_allowance(self):
        for smiles in ('CCB(O)O', 'CC[B-](F)(F)F.[K+]'):
            for signals, represented, missing in [('180.0', 1, 1), ('180.0 (1C)', 1, 1),
                                                   ('180.0 (2C)', 2, 0)]:
                with self.subTest(smiles=smiles, signals=signals):
                    result, tracker = self.analyze(smiles, 1, signals=signals)
                    self.assertEqual(tracker.get_count('c13_count_structure_check'), 0)
                    self.assertEqual(tracker.get_count('c13_boron_signal_not_observed'), int(bool(missing)))
                    self.assertEqual(result['carbon_scope']['full_environment_count'], 2)
                    self.assertEqual(result['carbon_scope']['represented_signal_count'], represented)
                    self.assertEqual(result['carbon_scope']['accepted_missing_direct_cb_count'], missing)
                    self.assertFalse(result['run_dp5'])
        for signals, cause in [('180.0 (2.5C)', 'invalid_carbon_annotation'),
                               ('180.0 (3C)', 'annotation_inventory_mismatch')]:
            result, tracker = self.analyze('CCB(O)O', 1, signals=signals)
            self.assertGreater(tracker.get_count('c13_count_structure_check'), 0)
            self.assertEqual(tracker.get_count('c13_boron_signal_not_observed'), 0)
            self.assertEqual(result['carbon_scope']['accepted_missing_direct_cb_count'], 0)
            self.assertEqual(result['dp5_refusal_details']['cause'], cause)
            self.assertFalse(result['run_dp5'])

    def test_multiple_direct_environments_do_not_invent_the_absent_site_identity(self):
        result, tracker = self.analyze('CCB(C)CCC', 5)
        scope = result['carbon_scope']
        self.assertEqual(scope['accepted_missing_direct_cb_count'], 1)
        self.assertEqual(scope['accepted_missing_direct_cb_environments'], [])
        self.assertEqual(scope['eligible_direct_cb_environments'], [['C2'], ['C4'], ['C5']])
        self.assertEqual(scope['direct_cb_atom_pairs'], [[1,2], [3,2], [4,2]])
        self.assertEqual(tracker.get_count('c13_count_structure_check'), 0)
        result, _ = self.analyze('CCB(C)CCC', 3)
        self.assertEqual(result['carbon_scope']['accepted_missing_direct_cb_count'], 3)
        self.assertEqual(result['carbon_scope']['accepted_missing_direct_cb_environments'], [['C2'], ['C4'], ['C5']])

    def test_unresolved_diagnostics_and_malformed_data_never_claim_accepted_absences(self):
        for smiles in ('CC[B-](F)(F)F.[K+].CCO', 'C[C@H](F)C[B-](F)(F)F.[K+] |&1:1|'):
            with self.subTest(smiles=smiles):
                result, tracker = self.analyze(smiles, 1)
                scope = result['carbon_scope']
                self.assertFalse(scope['partition_complete'])
                self.assertIsNone(scope['full_environment_count'])
                self.assertIsNotNone(scope['partition_reason'])
                self.assertEqual(scope['accepted_missing_direct_cb_count'], 0)
                self.assertEqual(tracker.get_count('c13_boron_signal_not_observed'), 0)
                self.assertEqual(tracker.get_count('c13_count_structure_check'), 0)
        for signals in ('180.0 170.0, 160.0', '180.0 (2.5C), 170.0, 160.0'):
            with self.subTest(signals=signals):
                result, tracker = self.analyze('CCB(C)CCC', 3, signals=signals)
                self.assertEqual(result['carbon_scope']['accepted_missing_direct_cb_count'], 0)
                self.assertEqual(tracker.get_count('c13_boron_signal_not_observed'), 0)
                self.assertEqual(tracker.get_count('nmr_signal_format'), 1)
                self.assertFalse(result['carbon_scope']['preparation_eligible'])


if __name__ == '__main__':
    unittest.main()
