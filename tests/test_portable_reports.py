"""Portable report contract at real renderer and filesystem boundaries."""
from concurrent.futures import ProcessPoolExecutor
import multiprocessing
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fitz

from checksi.config_schema import AppConfig
from checksi.report_service import export_reports


def config(directory):
    return AppConfig.model_validate({
        'paths': {'input_path': str(Path(directory) / 'source.PDF'), 'output_dir': directory},
        'modes': {'deep_name_analysis': 'off', 'output_mode': 'detailed'},
        'solvents': {'entries': []}, 'errors': {'default_severity': 'critical'},
    })


def analysis():
    return {'total_found': 1, 'error_stats': {}, 'compounds': [{
        'id': 1, 'source_paragraph_1based': 2, 'name': 'Ethanol', 'identifier': '1',
        'smiles': 'CCO', 'original_text': '¹H NMR: δ 7.28 ± 0.01 ppm; [M+H]⁺. <script>bad()</script>',
        'all_warnings': ['⚠️ Check δ and ¹³C.'],
        'highlight_info': {'color': 'red', 'hnmr_text': 'Proton data'},
        'dp5': {'status': 'success', 'score_percent': 9.99778, 'atoms': [
            {'label': 'C1', 'assigned_shift': 60.0, 'calculated_shift': 61.0, 'error': -0.0, 'score_percent': 80.0}]},
    }]}


def competing_export(arguments):
    cfg, data = arguments
    return export_reports(cfg, data, log_callback=lambda _: None)


class PortableReportTests(unittest.TestCase):
    def test_real_formats_preserve_semantics_fonts_and_source(self):
        with tempfile.TemporaryDirectory(prefix='reports δ .docx ') as tmp:
            cfg, data = config(tmp), analysis()
            cfg.dp5.score_threshold_percent = 10.
            before = deepcopy(data)
            with fitz.open() as source:
                source.new_page().insert_text((50, 50), 'Proton data')
                source.save(cfg.paths.input_path)
            output = export_reports(cfg, data, pdf_path=str(cfg.paths.input_path), log_callback=lambda _: None)
            self.assertEqual(data, before)
            self.assertFalse(output['partial'])
            root = Path(output['output_dir'])
            for key in ('html_path', 'pdf_path', 'analysis_path', 'source_pdf_path', 'error_stats_path'):
                self.assertEqual(Path(output[key]).parent, root)
                self.assertTrue(Path(output[key]).is_file())
            text = Path(output['html_path']).read_text(encoding="utf-8")
            self.assertIn('&lt;script&gt;', text)
            self.assertNotIn('<script>', text)
            self.assertIn('data:font/ttf;base64,', text)
            saved = json.loads(Path(output['analysis_path']).read_text(encoding="utf-8"))
            self.assertEqual(saved['analysis'], before)
            self.assertEqual(saved['source']['sha256'], __import__('hashlib').sha256(cfg.paths.input_path.read_bytes()).hexdigest())
            self.assertEqual(output['highlighted_compounds'], 0)
            self.assertTrue(saved['annotation_warnings'])
            self.assertEqual(saved['source_associations'], [])
            with fitz.open(output['pdf_path']) as rendered:
                text = ''.join(page.get_text() for page in rendered)
                for expected in ('δ', '¹³C', '9.998%', 'Corrected error', '60.00'):
                    self.assertIn(expected, text)
                self.assertTrue(any(page.get_images() for page in rendered))
                self.assertTrue(any('Noto' in font[3] for page in rendered for font in page.get_fonts()))

    def test_two_competing_exports_reserve_complete_distinct_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, data = config(tmp), analysis()
            sentinel = Path(tmp) / 'findings.pdf'
            sentinel.write_bytes(b'existing output')
            with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as workers:
                outputs = list(workers.map(competing_export, [(cfg, data), (cfg, data)]))
            self.assertNotEqual(outputs[0]['output_dir'], outputs[1]['output_dir'])
            self.assertEqual(sentinel.read_bytes(), b'existing output')
            for output in outputs:
                self.assertTrue(Path(output['analysis_path']).is_file())
                self.assertTrue(Path(output['html_path']).is_file())

    def test_optional_failures_retain_required_findings_and_truthful_metadata(self):
        for boundary in ('checksi.report_service.render_pdf', 'openpyxl.Workbook.save'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmp:
                with patch(boundary, side_effect=OSError('disk unavailable')):
                    output = export_reports(config(tmp), analysis(), log_callback=lambda _: None)
                self.assertTrue(output['partial'])
                self.assertTrue(output['artifact_warnings'])
                for key in ('html_path', 'analysis_path'):
                    self.assertTrue(Path(output[key]).is_file())
                for key in ('pdf_path', 'error_stats_path', 'source_pdf_path'):
                    if output[key]:
                        self.assertTrue(Path(output[key]).is_file())
                self.assertIn('disk unavailable', Path(output['html_path']).read_text(encoding="utf-8"))

    def test_cancellation_during_optional_export_is_retained_in_semantic_metadata(self):
        import threading
        import openpyxl
        with tempfile.TemporaryDirectory() as tmp:
            cfg, data = config(tmp), analysis()
            before = deepcopy(data)
            token = threading.Event()
            original_save = openpyxl.Workbook.save
            def save_then_cancel(workbook, destination):
                original_save(workbook, destination)
                token.set()
            with patch('openpyxl.Workbook.save', save_then_cancel):
                output = export_reports(cfg, data, cancel_token=token, log_callback=lambda _: None)
            saved = json.loads(Path(output['analysis_path']).read_text(encoding="utf-8"))
            self.assertTrue(saved['export']['aborted'])
            self.assertTrue(output['partial'])
            self.assertIsNone(output['pdf_path'])
            self.assertTrue(Path(output['error_stats_path']).is_file())
            self.assertEqual(data, before)

    def test_unmatched_source_annotations_are_informational_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, data = config(tmp), analysis()
            with fitz.open() as source:
                source.new_page().insert_text((50, 50), 'No matching source phrase')
                source.save(cfg.paths.input_path)
            before = deepcopy(data)
            output = export_reports(cfg, data, pdf_path=str(cfg.paths.input_path), log_callback=lambda _: None)
            self.assertEqual(data, before)
            self.assertFalse(output['partial'])
            self.assertTrue(output['annotation_warnings'])
            self.assertEqual(output['highlighted_compounds'], 0)
            self.assertTrue(Path(output['source_pdf_path']).is_file())
            text = Path(output['html_path']).read_text(encoding="utf-8")
            self.assertIn('associated 0 of 1 compounds', text)
            self.assertIn('unmatched IDs: 1', text)
            self.assertEqual(json.loads(Path(output['analysis_path']).read_text(encoding="utf-8"))['analysis'], before)

    def test_warning_text_cannot_fabricate_source_compound_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, data = config(tmp), analysis()
            data['compounds'][0]['all_warnings'].append('Extracted identifier includes comment ID 999.')
            with fitz.open() as source:
                source.new_page().insert_text((50, 50), 'Proton data')
                source.save(cfg.paths.input_path)
            output = export_reports(cfg, data, pdf_path=str(cfg.paths.input_path), log_callback=lambda _: None)
            self.assertEqual(output['highlighted_compounds'], 0)
            self.assertTrue(output['annotation_warnings'])
            self.assertEqual(output['source_associations'], [])

    def test_real_xlsx_encoding_failure_is_partial_and_records_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp)
            cfg.paths.input_path = Path(tmp) / 'source\x01.pdf'
            output = export_reports(cfg, analysis(), log_callback=lambda _: None)
            self.assertTrue(output['partial'])
            self.assertIsNone(output['error_stats_path'])
            self.assertTrue(Path(output['html_path']).is_file())
            self.assertTrue(Path(output['analysis_path']).is_file())

    def test_required_failure_keeps_successful_artifacts_and_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp)
            cfg.paths.input_path.write_bytes(b'user source')
            with patch('checksi.report_service.render_html', side_effect=ValueError('bad rendering')):
                with self.assertRaisesRegex(ValueError, 'bad rendering'):
                    export_reports(cfg, analysis())
            self.assertEqual(cfg.paths.input_path.read_bytes(), b'user source')
            self.assertTrue(list(Path(tmp).glob('*/results.json')))
