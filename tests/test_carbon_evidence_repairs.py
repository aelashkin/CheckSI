"""Carbon source refusals retain meaningful diagnostic identities and reasons."""

import unittest

from checksi.utils.error_handler import ErrorTracker
from tests.test_hrms import make_config


class CarbonDiagnosticRepairTests(unittest.TestCase):
    def test_without_structure_annotation_excess_keeps_formula_diagnostic_identity(self):
        from checksi.analysis.nmr import check_nmr
        for severity in ('disabled', 'info', 'warning', 'critical'):
            cfg = make_config(overrides={'c13_count_exceeds_formula': severity})
            tracker = ErrorTracker()
            checked = check_nmr(cfg, tracker, '13C NMR (100 MHz, CDCl3): δ 60.0 (4C), 20.0.',
                                '13C', 1, None, 400, 0, 0, 'C2H6O', 0, 0)
            self.assertEqual(tracker.get_count('c13_count_exceeds_formula'), int(severity != 'disabled'))
            self.assertEqual(tracker.get_count('c13_count_structure_check'), 0)
            self.assertFalse(checked[6])
            self.assertFalse(any('DP5q' in line for line in checked[0]))

    def test_invalid_annotation_message_names_source_token_without_double_period(self):
        from tests.test_carbon_observations import carbon
        checked, _, _ = carbon('CC(=O)C', 'C3H6O', '207.1, 30.9 (0C)')
        line, = [line for line in checked[0] if 'nmr_signal_format.' in line]
        self.assertIn('30.9 (0C)', line)
        self.assertNotIn('..', line)

    def test_annotation_refusal_has_its_own_reason_when_coverage_counts_match(self):
        from checksi.dp5_enrichment import DP5PreparationSkip, _compound_request
        from tests.test_carbon_observations import carbon, config
        details = {}
        checked, _, _ = carbon('CCOCC', 'C4H10O', '65.9 (2C), 15.4', dp5_refusal_details=details)
        compound = {'id': 1, 'smiles': 'CCOCC', 'dp5_c_signals_list': checked[7],
                    'dp5_carbon_groups': checked[5], 'run_dp5': checked[6], 'dp5_refusal_details': details}
        with self.assertRaises(DP5PreparationSkip) as caught:
            _compound_request(config(), compound, 1)
        self.assertEqual(caught.exception.reason, 'annotation_partition_mismatch')
        self.assertNotIn('coverage mismatch', caught.exception.warning_details)
        self.assertIn('annotation', caught.exception.warning_details)
