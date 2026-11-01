"""Charge notation and arithmetic follow Pavel's independent run3 checks."""

import unittest

from checksi.analysis.hrms import adjust_mass_by_hrms, check_hrms
from checksi.controller import CheckSIController
from checksi.utils.error_handler import ErrorTracker
from tests.test_hrms import make_config


SOURCE_119 = 'APCI [M+H] calcd for C21H29N2 309.2325; found 309.2302.'


class ConfirmedChargeNotationTests(unittest.TestCase):
    def test_1_119_infers_one_positive_charge_but_retains_both_notation_findings(self):
        tracker = ErrorTracker()
        details = {}
        output, formula, mass = check_hrms(make_config(), tracker, SOURCE_119, [], details=details)
        self.assertEqual(formula, 'C21H29N2')
        self.assertEqual(details['charge'], 1)
        self.assertEqual({k: v for k, v in tracker.to_dict().items() if v}, {
            'hrms_formula_charge_missing': 1, 'hrms_ion_charge_missing': 1})
        self.assertEqual(output, [
            '⚠️ hrms_formula_charge_missing. No charge sign was written on the HRMS formula.',
            '⚠️ hrms_ion_charge_missing. No charge sign was found in the ion formula (e.g. [M+H]+).',
        ])
        self.assertFalse(any('electron' in line for line in output))
        # Atom mass =21*12 +29*1.00782503223 +2*14.00307400443.
        self.assertAlmostEqual(mass, 309.23307394353, places=7)
        neutral = adjust_mass_by_hrms(make_config(), ErrorTracker(), SOURCE_119, [], formula, mass)
        self.assertEqual(neutral[1], 'C21H28N2')

    def test_disabled_notation_diagnostic_does_not_change_charge_or_formula_composition(self):
        records = []
        for formula_disabled, ion_disabled in ((False, False), (True, False), (False, True), (True, True)):
            tracker = ErrorTracker()
            cfg = make_config(overrides={
                'hrms_formula_charge_missing': 'disabled' if formula_disabled else 'warning',
                'hrms_ion_charge_missing': 'disabled' if ion_disabled else 'warning'})
            result = CheckSIController(cfg)._analyze_single_description(SOURCE_119, None, tracker)
            records.append(result['formula_sources']['hrms_neutral']['composition']['counts'])
            self.assertEqual(result['formula_sources']['hrms']['charge'], 1)
            self.assertEqual(tracker.get_count('hrms_formula_charge_missing'), int(not formula_disabled))
            self.assertEqual(tracker.get_count('hrms_ion_charge_missing'), int(not ion_disabled))
            for code in ('calcd_formula_mass_mismatch', 'calcd_formula_mass_delta'):
                self.assertEqual(tracker.get_count(code), 0, code)
        self.assertEqual(records, [{'C': 21, 'H': 28, 'N': 2}] * 4)

    def test_117_118_148_independently_report_each_missing_sign(self):
        for text, expected in (
                (SOURCE_119.replace('C21H29N2 ', 'C21H29N2+ '), 'hrms_ion_charge_missing'),
                (SOURCE_119.replace('[M+H]', '[M+H]+'), 'hrms_formula_charge_missing'),
                ('HRMS (ESI) m/z: [M]+ Calcd. for C27H22S2: 410.1157, found: 410.1164.',
                 'hrms_formula_charge_missing')):
            with self.subTest(text=text):
                tracker = ErrorTracker()
                details = {}
                check_hrms(make_config(), tracker, text, [], details=details)
                self.assertEqual(details['charge'], 1)
                self.assertEqual({k: v for k, v in tracker.to_dict().items() if v}, {expected: 1})

    def test_explicit_contradiction_still_reports_once_and_uses_formula_charge(self):
        tracker = ErrorTracker()
        details = {}
        text = 'APCI [M+H]- calcd for C21H29N2+ 309.2325; found 309.2302.'
        check_hrms(make_config(), tracker, text, [], details=details)
        self.assertEqual(details['charge'], 1)
        self.assertEqual({k: v for k, v in tracker.to_dict().items() if v}, {'hrms_charge_inconsistent': 1})


if __name__ == '__main__':
    unittest.main()
