"""Joined source descriptions use the extraction's body order and margins."""

from pathlib import Path
import tempfile
import unittest

import fitz

from checksi.file_io import extract_text_from_pdf
from checksi.report_service import export_reports
from tests.test_review_report_repairs import make_config


class SourceAnnotationStreamTests(unittest.TestCase):
    def fixture(self, root, columns=False):
        source = root / 'source.pdf'
        first = '1H NMR (400 MHz, CDCl3): 1.00 (s, 6H).'
        continuation = 'Anal. Calcd for C2H6O: C, 52.14; H, 13.13.'
        second = '1H NMR (400 MHz, CDCl3): 1.20 (s, 6H).'
        with fitz.open() as document:
            page = document.new_page()
            if columns:
                # Logical source order deliberately differs from vertical sort.
                page.insert_text((40, 180), first, fontsize=7)
                page.insert_text((310, 110), continuation, fontsize=7)
            else:
                page.insert_text((40, 110), first, fontsize=7)
                page = document.new_page()
                page.insert_text((40, 20), 'Supporting information S2', fontsize=7)
                page.insert_text((40, 110), continuation, fontsize=7)
            page.insert_text((40, 330), second, fontsize=7)
            document.save(source)
        compounds = [
            {'id': 1, 'original_text': first + ' ' + continuation,
             'highlight_info': {'hnmr_text': first, 'color': 'red'}, 'all_warnings': ['First only']},
            {'id': 2, 'original_text': second,
             'highlight_info': {'hnmr_text': second, 'color': 'blue'}, 'all_warnings': ['Second only']},
        ]
        return source, compounds

    def test_joined_page_and_column_continuations_keep_unique_source_ownership(self):
        for columns in (False, True):
            with self.subTest(columns=columns), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, compounds = self.fixture(root, columns)
                body = extract_text_from_pdf(str(source), header_margin=.08, footer_margin=.05)
                self.assertNotIn('Supporting information', body)
                cfg = make_config(root)
                cfg.modes.header_margin, cfg.modes.footer_margin = .08, .05
                result = export_reports(cfg, {'total_found': 2, 'error_stats': {}, 'compounds': compounds},
                                        pdf_path=str(source), log_callback=lambda _: None)
                self.assertEqual(result['highlighted_compounds'], 2)
                self.assertEqual({item['compound_id'] for item in result['source_associations']}, {1, 2})
                with fitz.open(result['source_pdf_path']) as document:
                    notes = [annotation.info['content'] for page in document for annotation in page.annots()
                             if annotation.info['title'] != 'stat' and annotation.info['content']]
                self.assertEqual(len(notes), 2)
                self.assertTrue(any('First only' in note and 'Second only' not in note for note in notes))
