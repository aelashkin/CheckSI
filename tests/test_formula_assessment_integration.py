"""Coverage follows complete scientific source candidates, including refusals."""

import unittest

from checksi.analysis.hrms_fields import find_ea_formula_candidate
from tests import test_assessment_boundaries as boundary_fixtures
from tests.test_assessment_boundaries import CARBON, EA, HRMS, PROTON


class FormulaAssessmentIntegrationTests(unittest.TestCase):
    def analyze(self, text, severity='critical'):
        return boundary_fixtures.AssessmentBoundaryTests().analyze(text, severity)

    def test_after_formula_percent_metadata_is_assessed_with_checks_enabled(self):
        block = EA.replace('C2H6O:', 'C2H6O (%):').replace(
            'Found: C, 40.00; H, 13.13.', 'Found: C, 40.00; H, 10.00.')
        for text in (' '.join((PROTON, CARBON, block)), ' '.join((block, PROTON, CARBON))):
            with self.subTest(text=text):
                enabled, disabled = self.analyze(text), self.analyze(text, 'disabled')
                self.assertEqual(enabled['assessment']['status'], 'complete')
                source = enabled['compounds'][0]['formula_sources']['ea']
                self.assertEqual(source['formula'], 'C2H6O')
                self.assertTrue(source['usable'])
                self.assertGreater(enabled['error_stats']['ea_tolerance_exceeded'], 0)
                self.assertEqual(enabled['error_stats']['formula_invalid_format'], 0)
                self.assertEqual(enabled['assessment'], disabled['assessment'])
                self.assertTrue(all(value == 0 for value in disabled['error_stats'].values()))

    def test_before_for_percent_header_remains_unmatched_and_unassessed(self):
        block = EA.replace('Calcd for', 'Calcd (%) for')
        self.assertIsNone(find_ea_formula_candidate(block))
        result = self.analyze(' '.join((PROTON, CARBON, block)))
        self.assertEqual(result['assessment']['status'], 'partial')
        self.assertTrue(any(record['reason'] == 'additional_analysis_section'
                            for record in result['assessment']['unassessed']))

    def test_explicit_formula_refusal_is_assessed_independently_of_finding_visibility(self):
        # Unsupported notation is a coverage gap; a definite author error is assessed.
        for source, block, raw, status, coverage in (
            ('ea', EA.replace('C2H6O:', 'C2H6O(H2O):'), 'C2H6O(H2O)', 'unsupported', 'partial'),
            ('hrms', HRMS.replace('C2H7O+', '[13C] C2H7O+'), '[13C] C2H7O', 'unsupported', 'partial'),
            ('ea', EA.replace('C2H6O:', 'C2H6[18O:'), 'C2H6[18O', 'invalid', 'complete'),
            ('hrms', HRMS.replace('C2H7O+', 'C2H7[18O+'), 'C2H7[18O', 'invalid', 'complete'),
        ):
            for text in (' '.join((PROTON, CARBON, block)), ' '.join((block, PROTON, CARBON))):
                with self.subTest(source=source, text=text):
                    enabled, disabled = self.analyze(text), self.analyze(text, 'disabled')
                    self.assertEqual(enabled['assessment']['status'], coverage)
                    record = enabled['compounds'][0]['formula_sources'][source]
                    self.assertEqual(record['status'], status)
                    self.assertEqual(record['candidate']['raw'], raw)
                    self.assertFalse(record['usable'])
                    self.assertEqual(enabled['error_stats']['formula_invalid_format'], int(status == 'invalid'))
                    self.assertEqual(enabled['assessment'], disabled['assessment'])
                    self.assertTrue(all(value == 0 for value in disabled['error_stats'].values()))

    def test_later_formula_block_is_unassessed_after_an_explicit_first_refusal(self):
        for first, second in (
            (EA.replace('C2H6O:', 'C2H6O(H2O):'), EA),
            (HRMS.replace('C2H7O+', '[13C] C2H7O+'), HRMS),
        ):
            with self.subTest(first=first):
                text = ' '.join((PROTON, CARBON, first, second))
                result = self.analyze(text)
                omission, formula = sorted(result['assessment']['unassessed'], key=lambda item: item['reason'])
                self.assertEqual(formula['reason'], 'formula_notation_not_supported')
                self.assertEqual(omission['reason'], 'additional_analysis_section')
                self.assertEqual(omission['source']['start_offset'], text.rindex(second))
                self.assertEqual(result['error_stats']['formula_invalid_format'], 0)
