"""Calculated-field punctuation retains complete scientific checks and refusals."""

import unittest

from checksi.analysis.hrms_fields import find_hrms_formula_candidate
from checksi.controller import CheckSIController
from tests.test_formula_reconciliation import NMR
from tests.test_hrms import make_config


CUES = ('calcd=', 'calcd:', 'calculated=', 'calculated:', 'calcd = ', 'calculated : ')


def description(cue: str, token: str = 'C3H9O') -> str:
    return NMR + f'HRMS {cue}{token}+ [M+H]+ 61.0000, found 61.0300.'


class CalculatedPunctuationTests(unittest.TestCase):
    def test_punctuation_after_calculated_cue_is_outside_formula_evidence(self):
        for cue in CUES:
            with self.subTest(cue=cue):
                text = description(cue)
                candidate = find_hrms_formula_candidate(text)
                self.assertIsNotNone(candidate)
                self.assertEqual(candidate.formula, 'C3H9O')
                self.assertEqual(text[candidate.start:candidate.end], 'C3H9O')

    def test_actual_controller_checks_mass_and_ppm_for_punctuated_cues(self):
        for cue in CUES:
            with self.subTest(cue=cue):
                result = CheckSIController(make_config(), event_dispatcher=lambda _: None).run_analysis([description(cue)])
                self.assertEqual(result['total_found'], 1)
                self.assertEqual(result['assessment']['status'], 'complete')
                source = result['compounds'][0]['formula_sources']['hrms']
                self.assertEqual(source['formula'], 'C3H9O')
                self.assertTrue(source['usable'])
                self.assertEqual(result['error_stats']['calcd_formula_mass_mismatch'], 1)
                self.assertEqual(result['error_stats']['hrms_ppm_too_large'], 2)
                self.assertEqual(result['error_stats']['formula_invalid_format'], 0)

    def test_punctuation_does_not_rescue_an_unsupported_formula_suffix(self):
        for cue in CUES[:4]:
            for raw in ('[13C] C3H9O', '2 C3H9O', 'xC3H9O', 'C3 H9O', 'C3H9O(H2O)'):
                with self.subTest(cue=cue, raw=raw):
                    text = description(cue, raw)
                    candidate = find_hrms_formula_candidate(text)
                    self.assertIsNotNone(candidate)
                    self.assertEqual(candidate.raw, raw)
                    self.assertIsNone(candidate.formula)
                    result = CheckSIController(make_config(), event_dispatcher=lambda _: None).run_analysis([text])
                    self.assertFalse(result['compounds'][0]['formula_sources']['hrms']['usable'])
                    # The unsupported notation is not checked rather than reported as an author error.
                    self.assertEqual(result['assessment']['status'], 'partial')
                    self.assertEqual(result['error_stats']['formula_invalid_format'], 0)
                    self.assertEqual(result['error_stats']['calcd_formula_mass_mismatch'], 0)
                    self.assertEqual(result['error_stats']['hrms_ppm_too_large'], 0)
