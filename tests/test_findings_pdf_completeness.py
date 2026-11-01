"""Table pagination must not silently discard complete compound cards."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fitz

from checksi.controller import CheckSIController
from checksi.report_rendering import render_html, render_pdf
from checksi.report_service import export_reports
from tests.test_hrms import make_config
from tests.test_formula_reconciliation import NMR, HRMS


def analysis_with_tables(count=30):
    compounds = []
    for number in range(1, count + 1):
        sources = {source: {'candidate': {'raw': 'C3H8O'}, 'formula': 'C3H8O',
            'status': 'valid', 'composition': {'state': 'neutral',
                'reason': 'Source evidence. ' * (number % 7 + 1)}}
            for source in ('ea', 'hrms', 'hrms_neutral', 'name')}
        compounds.append({'id': number, 'name': f'Marker{number}',
            'original_text': 'Source description. ' * ((number * 13) % 20 + 1),
            'all_warnings': ['formula_source_review_required. Review source basis.'],
            'formula_sources': sources, 'formula_comparisons': [{'left': 'ea',
                'right': 'hrms_neutral', 'status': 'agree', 'reason': 'Same complete composition. ' * 3}]})
    return {'total_found': count, 'error_stats': {}, 'compounds': compounds}


class FindingsPDFCompleteness(unittest.TestCase):
    def test_every_compound_heading_survives_table_page_boundaries(self):
        analysis = analysis_with_tables()
        markup, resources = render_html(make_config(), analysis, 'Summary',
                                       {'artifact_warnings': []}, {'name': 'fixture.pdf'})
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / 'findings.pdf'
            render_pdf(markup, resources, destination)
            with fitz.open(destination) as document:
                text = ''.join(page.get_text() for page in document)
        missing = [number for number in range(1, 31) if f'Compound {number}: Marker{number}' not in text]
        self.assertEqual(missing, [])

    def test_unnamed_compounds_are_headings_without_a_colon(self):
        analysis = analysis_with_tables(2)
        for compound in analysis['compounds']:
            compound['name'] = ''
        with tempfile.TemporaryDirectory() as temporary:
            cfg = make_config()
            cfg.paths.output_dir = Path(temporary)
            result = export_reports(cfg, analysis, log_callback=lambda _: None)
            self.assertFalse(result['partial'])
            with fitz.open(result['pdf_path']) as document:
                text = ''.join(page.get_text() for page in document)
            self.assertIn('Compound 1', text)
            self.assertIn('Compound 2', text)

    def test_refused_name_retains_its_identifier_and_a_complete_findings_pdf(self):
        with tempfile.TemporaryDirectory() as temporary:
            cfg = make_config()
            cfg.paths.output_dir = Path(temporary)
            analysis = CheckSIController(cfg).run_analysis([
                'Stirred ethanol (1a)', NMR + HRMS])
            self.assertEqual(analysis['compounds'][0]['name'], '')
            self.assertEqual(analysis['compounds'][0]['identifier'], '1a')
            self.assertEqual(analysis['compounds'][0]['name_resolution']['reason'], 'mixed_context')
            result = export_reports(cfg, analysis, log_callback=lambda _: None)
            self.assertFalse(result['partial'])
            with fitz.open(result['pdf_path']) as document:
                text = ''.join(page.get_text() for page in document)
            self.assertIn('Compound 1 (1a)', text)

    def test_a_body_reference_cannot_substitute_for_a_missing_compound_heading(self):
        def incomplete(*args, **kwargs):
            document = fitz.open()
            page = document.new_page()
            page.insert_text((40, 80), 'Compound 1: Marker1', fontsize=15, fontname='hebo')
            page.insert_text((40, 110), 'The source compares Compound 2 with Compound 1.')
            page.insert_text((40, 140), 'Compound 2: Marker2 appears as source prose, not a heading.')
            return document
        with tempfile.TemporaryDirectory() as temporary:
            cfg = make_config()
            cfg.paths.output_dir = Path(temporary)
            with patch('checksi.report_rendering.fitz.Story.write_with_links', side_effect=incomplete):
                result = export_reports(cfg, analysis_with_tables(2), log_callback=lambda _: None)
            self.assertTrue(result['partial'])
            self.assertIsNone(result['pdf_path'])
            self.assertTrue(Path(result['html_path']).is_file())
            self.assertTrue(Path(result['analysis_path']).is_file())

    def test_a_still_incomplete_pdf_is_not_advertised_as_a_complete_export(self):
        def incomplete(*args, **kwargs):
            document = fitz.open()
            document.new_page().insert_text((40, 80), 'Compound 1: Marker1')
            return document
        with tempfile.TemporaryDirectory() as temporary:
            cfg = make_config()
            cfg.paths.output_dir = Path(temporary)
            with patch('checksi.report_rendering.fitz.Story.write_with_links', side_effect=incomplete):
                result = export_reports(cfg, analysis_with_tables(2), log_callback=lambda _: None)
            self.assertTrue(result['partial'])
            self.assertIsNone(result['pdf_path'])
            self.assertTrue(Path(result['html_path']).is_file())
            self.assertTrue(Path(result['analysis_path']).is_file())
