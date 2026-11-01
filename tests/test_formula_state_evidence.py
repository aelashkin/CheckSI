"""Preserve unsupported charge and pre-normalization scientific state evidence."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from checksi.analysis import hrms_fields
from checksi.analysis.hrms import adjust_mass_by_hrms, check_hrms
from checksi.analysis.hrms_fields import find_hrms_formula_candidate
from checksi.controller import CheckSIController
from checksi.report_rendering import _formula_html
from checksi.report_service import export_reports
from checksi.utils.error_handler import ErrorTracker
from tests.test_formula_reconciliation import EA_ETHANOL, EA_PROPANOL, HRMS, NMR
from tests.test_hrms import make_config


class FormulaStateEvidenceTests(unittest.TestCase):
    def evaluate(self, hrms=HRMS, ea=EA_PROPANOL, severity='warning'):
        cfg = make_config(overrides={'formula_source_review_required': severity, 'hrms_charge_inconsistent': 'disabled'})
        tracker = ErrorTracker()
        result = CheckSIController(cfg)._analyze_single_description(NMR + hrms + ea, 'CCCO', tracker)
        return result, tracker

    def test_complete_formula_side_charge_is_retained_before_state_qualification(self):
        for suffix in ('+2', '-2', '+1', '+02', '++', '+-'):
            with self.subTest(suffix=suffix):
                text = HRMS.replace('C3H9O+', 'C3H9O' + suffix)
                candidate = find_hrms_formula_candidate(text)
                self.assertEqual(candidate.formula, 'C3H9O')
                self.assertEqual(candidate.charge_raw, suffix)
                self.assertEqual(text[candidate.charge_start:candidate.charge_end], suffix)
                self.assertEqual(candidate.charge_supported, suffix in ('+2', '-2', '+1'))
                details = {}
                _, formula, mass = check_hrms(make_config(), ErrorTracker(), text, [], details=details)
                self.assertEqual(formula, 'C3H9O')
                self.assertTrue(mass)
                self.assertEqual(details['candidate']['charge_raw'], suffix)
                inverse = {}
                _, neutral, _ = adjust_mass_by_hrms(make_config(), ErrorTracker(), text, [], formula, mass, details=inverse)
                self.assertEqual(neutral, 'C3H8O')
                self.assertEqual(inverse['comparable'], suffix == '+1')

    def test_unit_charge_and_adjacent_numeric_mass_keep_their_existing_basis(self):
        for tail in ('+ [M+H]+ 61.0648, found 61.0648.', '+61.0648, found 61.0648.', '+ 47, found 47.0491.'):
            candidate = find_hrms_formula_candidate('HRMS calcd for C3H9O' + tail)
            self.assertEqual(candidate.formula, 'C3H9O')
            self.assertEqual(candidate.charge_raw, '+')
            self.assertTrue(candidate.charge_supported)
        result, tracker = self.evaluate()
        self.assertEqual([pair['status'] for pair in result['formula_comparisons']], ['agree'] * 3)
        self.assertEqual(tracker.get_count('formula_source_review_required'), 0)

    def test_unsupported_formula_charge_never_establishes_neutral_agreement(self):
        for severity in ('disabled', 'info', 'warning', 'critical'):
            for suffix in ('+02', '++', '+-'):
                with self.subTest(severity=severity, suffix=suffix):
                    result, tracker = self.evaluate(hrms=HRMS.replace('C3H9O+', 'C3H9O' + suffix), severity=severity)
                    source = result['formula_sources']['hrms_neutral']
                    self.assertEqual(source['formula'], 'C3H8O')
                    self.assertFalse(source['comparable'])
                    self.assertEqual(source['composition']['state'], 'unknown')
                    self.assertEqual([pair['status'] for pair in result['formula_comparisons']], ['not_comparable', 'agree', 'not_comparable'])
                    self.assertEqual(tracker.get_count('formula_source_mismatch'), 0)
                    self.assertEqual(tracker.get_count('name_formula_mismatch'), 0)
                    self.assertEqual(tracker.get_count('formula_source_review_required'), int(severity != 'disabled'))
                    if severity == 'disabled' and suffix != '-2':
                        self.assertEqual(_formula_html(result), '')
                    else:
                        self.assertIn(suffix, _formula_html(result))

    def test_multiple_owned_adduct_assertions_cannot_certify_first_inverse(self):
        for ions in ('[M+H]+ [M-H]-', '[M+H]+ [M+Na]+'):
            for severity in ('disabled', 'warning'):
                result, tracker = self.evaluate(hrms=HRMS.replace('[M+H]+', ions), severity=severity)
                neutral = result['formula_sources']['hrms_neutral']
                self.assertEqual(neutral['formula'], 'C3H8O')
                self.assertFalse(neutral['comparable'])
                self.assertEqual(neutral['composition']['state'], 'unknown')
                self.assertEqual(tracker.get_count('formula_source_mismatch'), 0)
                self.assertEqual(tracker.get_count('formula_source_review_required'), int(severity != 'disabled'))

    def test_every_owned_adduct_inverting_to_one_neutral_keeps_the_comparison(self):
        listings = (
            'HRMS (ESI) m/z: [M+H]+ calcd for C3H9O+ 61.0648, found 61.0648; '
            '[M+Na]+ calcd for C3H8NaO+ 83.0467, found 83.0467. ',
            'HRMS (ESI) calcd for C3H9O+ [M+H]+ 61.0648, found 61.0648; '
            'calcd for C3H8ONa+ [M+Na]+ 83.0467, found 83.0467. ',
            'HRMS (ESI) m/z: [M+H]+ calcd for C3H9O+ 61.0648; found [M+H]+ 61.0648. ',
        )
        for hrms in listings:
            with self.subTest(hrms=hrms):
                result, tracker = self.evaluate(hrms=hrms, severity='critical')
                neutral = result['formula_sources']['hrms_neutral']
                self.assertEqual(neutral['formula'], 'C3H8O')
                self.assertTrue(neutral['comparable'])
                self.assertEqual([pair['status'] for pair in result['formula_comparisons']], ['agree'] * 3)
                self.assertEqual(tracker.get_count('formula_source_review_required'), 0)
        for second in ('[M+Na]+ calcd for C4H10NaO+ 97.0624', '[M+K]+ calcd for C3H8NaO+ 83.0467',
                       '[M+Na]+ calcd for C3H8NaO+2 83.0467', '[2M+Na]+ calcd for C6H16NaO2+ 143.1043'):
            with self.subTest(second=second):
                hrms = f'HRMS (ESI) m/z: [M+H]+ calcd for C3H9O+ 61.0648, found 61.0648; {second}, found 1.0. '
                result, tracker = self.evaluate(hrms=hrms, severity='critical')
                self.assertFalse(result['formula_sources']['hrms_neutral']['comparable'])
                self.assertEqual(tracker.get_count('formula_source_review_required'), 1)

    def test_all_recognized_additional_ion_forms_block_neutral_agreement(self):
        for extra in ('[2M+H]+', '(2M+H)+', '([2M+H]+)', '(ESI, [2M+H]+)', '[2M+H',
                      '[M+[13C]H3]+', '[C3H9O]+', '[C2[13C]H9O]+'):
            for severity in ('disabled', 'warning'):
                with self.subTest(extra=extra, severity=severity):
                    result, tracker = self.evaluate(hrms=HRMS.replace('[M+H]+', '[M+H]+ ' + extra), severity=severity)
                    neutral = result['formula_sources']['hrms_neutral']
                    self.assertEqual(neutral['formula'], 'C3H8O')
                    self.assertFalse(neutral['comparable'])
                    self.assertEqual(neutral['composition']['state'], 'unknown')
                    self.assertEqual(tracker.get_count('formula_source_review_required'), int(severity != 'disabled'))
        details = {}
        text = 'HRMS C2H7[13C]O+ (ESI, [M+[13C]H3]+) 62.0000, found 62.0000.'
        _, formula, _ = adjust_mass_by_hrms(make_config(), ErrorTracker(), text, [], 'C2H7[13C]O', 0, details=details)
        self.assertEqual(formula, 'C2H4O')
        self.assertFalse(details['comparable'])

    def test_shared_wrapper_does_not_hide_distinct_ion_assertions(self):
        for ions in ('([M+H]+ [2M+H]+)', '(ESI, [M+H]+ (2M+H)+)', '([M+H]+ [M+[13C]H3]+)'):
            result, _ = self.evaluate(hrms=HRMS.replace('[M+H]+', ions))
            neutral = result['formula_sources']['hrms_neutral']
            self.assertEqual(neutral['formula'], 'C3H8O')
            self.assertFalse(neutral['comparable'])

    def test_failed_charge_run_is_consumed_once_without_losing_raw_evidence(self):
        raw = 'C2H6O' + '+' * 4000 + 'opaque'
        text = 'HRMS calcd for ' + raw + ' 100.0000, found 100.0000.'
        with patch.object(hrms_fields, '_charge_suffix', wraps=hrms_fields._charge_suffix) as charge:
            candidate = find_hrms_formula_candidate(text)
        self.assertIsNone(candidate.formula)
        self.assertEqual(candidate.raw, raw)
        self.assertEqual(text[candidate.start:candidate.end], raw)
        self.assertLessEqual(charge.call_count, 3, 'One malformed run must not trigger one suffix scan per sign.')

    def test_ea_component_separator_preserves_original_evidence_and_parent_check(self):
        for separator in ('•', 'ꞏ'):
            for severity in ('disabled', 'info', 'warning', 'critical'):
                raw = 'C2H6O' + separator + 'H2O'
                text = NMR + HRMS + EA_ETHANOL.replace('C2H6O:', raw + ':')
                result, tracker = self.evaluate(ea=EA_ETHANOL.replace('C2H6O:', raw + ':'), severity=severity)
                source = result['formula_sources']['ea']
                self.assertEqual(source['formula'], 'C2H6O·H2O')
                self.assertTrue(source['usable'])
                original = source['normalization']['pre_normalization_candidate']
                self.assertEqual(original['raw'], raw)
                self.assertEqual(original['span_basis'], 'original_description')
                self.assertEqual(text[original['start']:original['end']], raw)
                self.assertEqual(source['composition']['state'], 'composite')
                self.assertEqual(source['composition']['counts'], {'C': 2, 'H': 8, 'O': 2})
                self.assertEqual([pair['status'] for pair in result['formula_comparisons']], ['disagree', 'disagree', 'agree'])
                self.assertEqual(tracker.get_count('formula_source_mismatch'), 1)
                self.assertEqual(tracker.get_count('formula_source_review_required'), 0)
                self.assertGreater(tracker.get_count('ea_formula_calcd_mismatch'), 0)
                self.assertIn(raw, _formula_html(result))

    def test_hrms_deleted_separator_cannot_leave_neutral_stage_comparable(self):
        raw = 'C3H9O•H2O'
        result, tracker = self.evaluate(hrms=HRMS.replace('C3H9O', raw))
        source = result['formula_sources']['hrms_neutral']
        self.assertEqual(source['formula'], 'C3H10O2')
        self.assertFalse(source['comparable'])
        self.assertEqual(source['state'], 'unknown')
        self.assertEqual(source['normalization']['pre_normalization_candidate']['raw'], raw)
        self.assertEqual(tracker.get_count('formula_source_mismatch'), 0)

    def test_disabled_diagnostics_leave_charge_and_literal_source_in_semantic_report(self):
        cfg = make_config(overrides={'formula_source_review_required': 'disabled', 'hrms_charge_inconsistent': 'disabled'})
        description = NMR + HRMS.replace('C3H9O+', 'C3H9O+2') + EA_ETHANOL.replace('C2H6O:', 'C2H6O•H2O:')
        def resolve(config, tracker, text, stats, **kwargs):
            kwargs['details'].update(status='resolved', original_name='propan-1-ol', accepted_name='propan-1-ol')
            return 'CCCO', 'propan-1-ol', '1', stats, []
        with patch('checksi.controller._smiles_and_name_from_paragraph', side_effect=resolve):
            analysis = CheckSIController(cfg).run_analysis(['propan-1-ol (1)', description])
        before = deepcopy(analysis)
        with tempfile.TemporaryDirectory() as temporary:
            cfg.paths.output_dir = Path(temporary)
            output = export_reports(cfg, analysis, log_callback=lambda _: None)
            saved = json.loads(Path(output['analysis_path']).read_text())
            self.assertEqual(saved['analysis'], before)
            compound = saved['analysis']['compounds'][0]
            self.assertEqual(compound['original_text'], description)
            self.assertEqual(compound['formula_sources']['hrms']['candidate']['charge_raw'], '+2')
            html = Path(output['html_path']).read_text()
            self.assertIn('+2', html)
            self.assertIn('C2H6O•H2O', html)
            self.assertIn('not_comparable', html)
        self.assertEqual(analysis, before)


if __name__ == '__main__':
    unittest.main()
