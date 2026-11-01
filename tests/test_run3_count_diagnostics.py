"""Pavel's run3 count findings and requested DP5 refusal presentation."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lxml import html
from openpyxl import load_workbook

from checksi.controller import CheckSIController
from checksi.config_schema import ErrorSeverity
from checksi.dp5_enrichment import enrich_analysis_with_dp5, prepare_dp5_requests, DP5PreparationSkip
from checksi.dp5q.contracts import CheckRequest
from checksi.report_service import export_reports
from checksi.utils.error_handler import ErrorTracker
from tests.test_dp5_review_fixes import review_config


CASES = json.loads((Path(__file__).parent / 'fixtures/run3_count_sources.json').read_text(encoding='utf-8'))['cases']


class Run3CountDiagnosticsTests(unittest.TestCase):
    def analyze(self, case, *, enabled=True, disabled=False):
        cfg = review_config()
        cfg.dp5.enabled = enabled
        if disabled:
            cfg.errors.overrides['c13_count_structure_check'] = ErrorSeverity.DISABLED
        with patch('checksi.controller._smiles_and_name_from_paragraph',
                   return_value=(case['smiles'], 'Source compound', case['identifier'], {}, [])):
            data = CheckSIController(cfg).run_analysis(['Source compound', case['original_text']])
        return cfg, data

    def test_exact_sources_keep_two_count_findings_without_preparation_duplicates(self):
        for case in CASES:
            for enabled in (False, True):
                with self.subTest(identifier=case['identifier'], enabled=enabled):
                    _, data = self.analyze(case, enabled=enabled)
                    compound = data['compounds'][0]
                    count_lines = [line for line in compound['all_warnings'] if 'c13_count_structure_check.' in line]
                    self.assertEqual(len(count_lines), 2, count_lines)
                    self.assertTrue(any('diastereotopic' in line for line in count_lines))
                    observed, expected = (26, 13) if case['identifier'] == '1.41' else (
                        12 if case['identifier'] == '1.73' else 10, 9)
                    self.assertTrue(any(f'{observed}; probably, it should be {expected}' in line for line in count_lines))
                    self.assertEqual(compound['carbon_scope']['represented_signal_count'], observed)
                    self.assertFalse(compound['run_dp5'])
                    self.assertEqual(data['error_stats']['c13_count_exceeds_formula'], 0)
                    self.assertNotIn('dp5', compound)

    def test_requested_mismatch_is_info_before_readiness_and_visibility_independent(self):
        for case in CASES:
            for disabled in (False, True):
                with self.subTest(identifier=case['identifier'], disabled=disabled):
                    self.check_requested_skip(case, disabled)

    def check_requested_skip(self, case, disabled):
        cfg, data = self.analyze(case, disabled=disabled)
        before = deepcopy(data)
        prepared = prepare_dp5_requests(cfg, data)
        self.assertEqual(data, before)
        self.assertIsInstance(prepared[0], DP5PreparationSkip)
        with (patch('checksi.dp5q.client.check_availability', side_effect=AssertionError('no readiness')),
              patch('checksi.dp5q.client.DP5qSession.check', side_effect=AssertionError('no scoring'))):
            enrich_analysis_with_dp5(cfg, data, prepared_requests=prepared)
        result = data['compounds'][0]['dp5']
        self.assertEqual(result['status'], 'skipped')
        scope = data['compounds'][0]['carbon_scope']
        self.assertEqual(result['message'],
            f"DP5q not performed: the number of 13C signals ({scope['represented_signal_count']}) "
            f"does not match the expected number ({scope['full_environment_count']}).")
        self.assertEqual(result['message_severity'], 'info')
        self.assertNotIn('score_raw', result)
        self.assertEqual(data['error_stats']['dp5_analysis_failed'], 0)
        self.assertEqual(data['error_stats']['c13_count_structure_check'], 0 if disabled else 2)
        self.assertEqual(data['dp5_summary']['skipped'], 1)

    def test_formula_inventory_is_a_fallback_without_structure(self):
        case = CASES[0]
        cfg = review_config()
        result = CheckSIController(cfg)._analyze_single_description(case['original_text'], None, ErrorTracker())
        lines = result['warnings']
        self.assertTrue(any('c13_count_exceeds_formula.' in line and '26' in line and '17' in line for line in lines))
        self.assertFalse(any('c13_count_structure_check.' in line for line in lines))

    def test_same_structure_with_nine_signals_remains_eligible(self):
        case = {**CASES[1], 'original_text': CASES[1]['original_text'].replace('144.4 (2C)', '144.4')}
        cfg, data = self.analyze(case)
        self.assertTrue(data['compounds'][0]['run_dp5'])
        self.assertIsInstance(prepare_dp5_requests(cfg, data)[0], CheckRequest)
        self.assertEqual(data['error_stats']['c13_count_structure_check'], 0)

    def test_boron_refusal_precedes_count_info(self):
        cfg = review_config()
        compound = CheckSIController(cfg)._analyze_single_description(
            '13C NMR (100 MHz, CDCl3): δ 20.0 (2C).', 'CB(O)O', ErrorTracker())
        result, = prepare_dp5_requests(cfg, {'compounds': [{**compound, 'smiles': 'CB(O)O'}]})
        self.assertIsInstance(result, DP5PreparationSkip)
        self.assertEqual(result.reason, 'boron_not_supported')
        self.assertIsNone(result.info_message)

    def test_info_survives_html_json_xlsx_in_both_report_modes(self):
        for mode in ('essential', 'detailed'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                cfg, data = self.analyze(CASES[1])
                cfg.paths.output_dir = Path(directory)
                cfg.modes.output_mode = mode
                enrich_analysis_with_dp5(cfg, data)
                note = data['compounds'][0]['dp5']['message']
                before = deepcopy(data)
                metadata = export_reports(cfg, data, log_callback=lambda message: None)
                self.assertEqual(data, before)
                document = html.fromstring(Path(metadata['html_path']).read_text(encoding='utf-8'))
                self.assertIn(note, [item.strip() for item in document.xpath('//p[@class="info"]//text()')])
                saved = json.loads(Path(metadata['analysis_path']).read_text(encoding='utf-8'))
                self.assertEqual(saved['analysis']['compounds'][0]['dp5']['message'], note)
                workbook = load_workbook(metadata['error_stats_path'], read_only=True)
                rows = list(workbook['Carbon scope'].values)
                self.assertEqual(rows[1][rows[0].index('DP5 message')], note)
                self.assertEqual(list(workbook['DP5 assignments'].values)[1:], [])
                workbook.close()


if __name__ == '__main__':
    unittest.main()
