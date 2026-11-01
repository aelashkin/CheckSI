"""Confirmed proton-descriptor and shift-token chemistry acceptance cases."""

import unittest

from checksi.analysis.nmr import _check_constant_hnmr, check_nmr
from checksi.utils.error_handler import ErrorTracker
from tests.test_name_resolver import make_config


class ProtonDescriptorAcceptanceTests(unittest.TestCase):
    def test_first_1_62_bracket_is_missing_only_with_other_members_independent(self):
        findings = _check_constant_hnmr(make_config(), ErrorTracker(),
            'δ 7.80 (J = 8.0 Hz, 4H), 6.59 (d, J = 8.4 Hz, 4H), '
            '2.28-2.22 (, 8H), 2.17-2.11 (n, 1H), 1.91-1.84 (2H)')
        self.assertEqual({f.code.value: f.context['brackets'] for f in findings}, {
            'hnmr_multiplicity_missing': '(J = 8.0 Hz, 4H), (, 8H), (2H)',
            'hnmr_wrong_multiplicity': '(n, 1H)',
            'hnmr_signal_desc_invalid': '(, 8H), (n, 1H), (2H)',
        })

    def test_1_64_recognized_d_has_only_its_independent_order_finding(self):
        findings = _check_constant_hnmr(make_config(), ErrorTracker(),
            'δ 7.82 (4H, d, J = 8.0 Hz), 6.59 (J = 8.4 Hz, 4H, d), '
            '3.12-3.04 (z, 4H, d, J = 8.0 Hz)')
        self.assertEqual({f.code.value: f.context['brackets'] for f in findings}, {
            'hnmr_signal_desc_invalid': '(4H, d, J = 8.0 Hz), (J = 8.4 Hz, 4H, d), (z, 4H, d, J = 8.0 Hz)',
        })

    def test_integral_before_j_remains_an_order_error_when_multiplicity_is_missing(self):
        findings = _check_constant_hnmr(make_config(), ErrorTracker(), 'δ 7.80 (4H, J = 8.0 Hz)')
        self.assertEqual({f.code.value for f in findings}, {
            'hnmr_multiplicity_missing', 'hnmr_signal_desc_invalid',
        })


class ExternalProtonIntegralTests(unittest.TestCase):
    def check(self, signals):
        tracker = ErrorTracker()
        result = check_nmr(make_config(), tracker,
            f'1H NMR (400 MHz, CDCl3): δ {signals}.',
            '1H', 0, None, 400, 0, 0, None, 0, 0)
        return tracker, result

    def test_literal_1_75_integrals_never_enter_ppm_checks(self):
        tracker, _ = self.check('7.95 (d, J = 8.0 Hz) 4H, 6.59 (d, J = 8.4 Hz) 4H, '
            '3.44 (br. s) 2H, 3.12-3.04 (m) 4H, 2.28-2.22 (m) 8H, '
            '2.17-2.11 (m) 1H, 1.91-1.84 (m) 2H, 1.47-1.38 (m) 2H, 1.03-0.95 (m) 1H')
        for code in ('nmr_signal_precision_invalid', 'nmr_signal_order_invalid', 'hnmr_duplicate_signals'):
            self.assertEqual(tracker.get_count(code), 0, code)
        self.assertEqual(tracker.get_count('nmr_signal_format'), 8)
        self.assertEqual(tracker.get_count('hnmr_h_count_missing'), 1)

    def test_real_shift_precision_order_and_duplicates_are_retained(self):
        tracker, _ = self.check('3.4 (s) 4H, 7.95 (m) 4H, 7.95 (m) 2H')
        for code in ('nmr_signal_precision_invalid', 'nmr_signal_order_invalid', 'hnmr_duplicate_signals'):
            self.assertEqual(tracker.get_count(code), 1, code)

    def test_spaced_decimal_and_cyrillic_integrals_are_not_shifts(self):
        tracker, _ = self.check('7.95 (m) 1.5 H, 6.59 (m) 2Н, 3.44 (m) 1.5 H')
        for code in ('nmr_signal_precision_invalid', 'nmr_signal_order_invalid', 'hnmr_duplicate_signals'):
            self.assertEqual(tracker.get_count(code), 0, code)


if __name__ == '__main__':
    unittest.main()
