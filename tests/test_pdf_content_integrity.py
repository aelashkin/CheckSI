"""PDF success requires complete owned text, table cells and structure images."""

from copy import deepcopy
import base64
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import fitz

from checksi.config_schema import AppConfig, ErrorSeverity
from checksi.report_rendering import _formula_html, render_html, render_pdf
from checksi.report_service import export_reports


def configuration(root):
    return AppConfig.model_validate({
        'paths': {'input_path': str(root / 'source.pdf'), 'output_dir': str(root / 'out')},
        'modes': {'deep_name_analysis': 'off', 'output_mode': 'detailed'},
        'solvents': {'entries': []}, 'errors': {'default_severity': 'warning'},
    })


def findings(*, count=2, dp5_rows=0):
    compounds = []
    for number in range(1, count + 1):
        compound = {
            'id': number, 'identifier': f'{number}a', 'original_text': (
                f'Owned source {number}: ¹H NMR δ 7.20; [M+H]⁺; C₆H₆. '
                'A body reference to Compound 999 is source prose.'),
            'all_warnings': [f'Owned observation {number}: interior evidence.',
                             'A repeated diagnostic must appear once for every owner.',
                             f'Owned observation {number}: final evidence.'],
        }
        if dp5_rows:
            compound['dp5'] = {'status': 'success', 'score_percent': 7.5, 'atoms': [
                {'label': f'C{number}_{index:03d}', 'assigned_shift': 300 - index,
                 'calculated_shift': 299 - index, 'error': 1.0, 'score_percent': 80.0}
                for index in range(dp5_rows)]}
        compounds.append(compound)
    return {'total_found': count, 'error_stats': {}, 'compounds': compounds}


def detailed_card_17_and_18():
    """Frozen renderer inputs from the tracked test_all_errors.docx sample.

    This fixes pagination geometry, not the scientific accuracy of its notes.
    """
    first = {'id': 17,
     'name': "Сorrect description N,N'-(((1R,3S)-cyclopentane-1,3-diyl)bis(methylene))bis(4-methylaniline)",
     'identifier': '5.2',
     'source_paragraph_1based': 47,
     'original_text': '1H NMR (400 MHz, CDCl3) δ 7.02 (d, J = 8.0 Hz, 4H), 6.59 (d, J = 8.4 Hz, 4H), 3.44 '
                      '(br. s, 2H), 3.12–3.04 (m, 4H), 2.28–2.22 (m, 8H), 2.17–2.11 (m, 1H), 1.91–1.84 (m, '
                      '2H), 1.47–1.38 (m, 2H), 1.03–0.95 (m, 1H); 13C NMR (101 MHz, CDCl3) δ 142.9, 129.7, '
                      '126.7, 113.1, 50.1, 39.5, 36.3, 29.6 (2C).APCI [M+H]+ calcd for C21H29N2+ 309.2325; '
                      'found 309.2302. ',
     'all_warnings': ['parsed name: Сorrect description '
                      "N,N'-(((1R,3S)-cyclopentane-1,3-diyl)bis(methylene))bis(4-methylaniline) (5.2)",
                      'the charges match. charges: +, +',
                      'calcd mass match formula',
                      'mass data ppm: 7.44 ppm',
                      'calcd mass: 309.2325',
                      'found mass: 309.2302',
                      'HRMS mass (from formula): 309.2325 (C21H29N2)',
                      'formula HRMS C21H29N2, real formula C21H28N2',
                      'The presence of H NMR constants has been verified',
                      'Extracted content solventfr 1H: (400 MHz, CDCl3)',
                      '¹H peaks for CDCl3 not found in textnmr.',
                      'H NMR Result: 28.0H',
                      'Order 1H NMR signals ok',
                      'Num H in 1H NMR ok (name, HRMS or EA). In 1H NMR 28.0H, in formula from name/HRMS/EA '
                      '28H, 28.0 = 28',
                      '⚠️ bad_ending_nmr. Section 13C NMR likely has an unsupported ending format. '
                      "Supported endings are '.' or ';'.",
                      'The presence of 13C NMR constants has been verified',
                      'Extracted content solventfr 13C: (101 MHz, CDCl3)',
                      '¹³C peaks for CDCl3 not found in textnmr.',
                      '⚠️ nmr_signal_format. Invalid 13C NMR signal formatting near: 29.6 (2C).APCI [M+H]+ '
                      'calcd for C21.',
                      '⚠️ nmr_signal_format. Invalid 13C NMR signal formatting near: 21H29.',
                      '⚠️ nmr_signal_format. Invalid 13C NMR signal formatting near: 29N2.',
                      '⚠️ nmr_signal_format. Invalid 13C NMR signal formatting near: 2+ 309.2325.',
                      '⚠️ nmr_signal_order_invalid. The order of 13C NMR signals is incorrect. Wrong signal '
                      'order: [142.9, 129.7, 126.7, 113.1, 50.1, 39.5, 36.3, 29.6, 21.0, 2.0, 309.2325]',
                      '⚠️ nmr_signal_rounding_needed. Signals with one more digit after the decimal point '
                      "than allowed were found in 13C NMR. It is likely that ['21', '29'] need rounding.",
                      '⚠️ nmr_signal_precision_invalid. Signals with more than 2 or fewer than 1 digits '
                      "after the decimal point were found in 13C NMR. It is likely that ['21', '29', '2', "
                      "'309.2325'] need rounding.",
                      '❗ C NMR Result: 9. Signals: [>170] 1, [120-170] 3, [80-120] 1, [<80] 4',
                      '❗ Check equivalent  C: 21 - 9 = 12 ',
                      'Stereodescriptors are of the same type.',
                      'stereo_check_required. Fraction of equivalent carbons = 0.57. Verify whether the '
                      'compound is enantiopure or meso. The compound is likely meso. If the compound is '
                      'chiral, ensure that chiral HPLC or optical rotation data is provided.']}
    second = deepcopy(first)
    second.update(id=18, name="", identifier="", source_paragraph_1based=first.get("source_paragraph_1based", 0) + 1)
    second["all_warnings"] = ["⚠️ name_not_found. Name was not found or unsupported compound number format."] + first["all_warnings"][1:-2]
    return {"total_found": 2, "error_stats": {}, "compounds": [first, second]}


def text(document):
    # MuPDF may draw a ligature and insert soft hyphens at layout breaks.
    value = ''.join(page.get_text(flags=fitz.TEXTFLAGS_TEXT & ~fitz.TEXT_PRESERVE_LIGATURES)
                    for page in document)
    return re.sub(r'\s+', '', value).replace('\u00ad', '').replace('\u200b', '')


def redact(document, phrase, *, once=False):
    for page in document:
        rectangles = page.search_for(phrase)
        for rectangle in rectangles[:1] if once else rectangles:
            page.add_redact_annot(rectangle)
        page.apply_redactions(images=0)
        if once and rectangles:
            return


class PDFContentIntegrityTests(unittest.TestCase):
    def test_real_card_17_shape_retains_every_owned_row_after_the_lossy_vendor_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cfg = configuration(root)
            cfg.errors.default_severity = ErrorSeverity.CRITICAL
            cfg.errors.overrides['stereo_check_required'] = ErrorSeverity.INFO
            data = detailed_card_17_and_18()
            markup, resources = render_html(cfg, data, 'S', {'artifact_warnings': []},
                                           {'name': 'test_all_errors.docx'})
            archive = fitz.Archive()
            legacy = markup.replace('<wbr>', '&#8203;')
            for name, value in resources.items():
                archive.add((value, name))
                legacy = legacy.replace('data:font/ttf;base64,' + base64.b64encode(value).decode(), name)
            legacy = legacy.replace('</head>', '<style>.compound {page-break-before: always;}</style></head>')
            original = fitz.Story.write_with_links
            calls = []

            def first_pass_uses_lossy_vendor_layout(story, *args, **kwargs):
                calls.append(1)
                # Exercise the real pinned renderer bug at the external boundary.
                if len(calls) == 1:
                    return original(fitz.Story(legacy, archive=archive), *args, **kwargs)
                return original(story, *args, **kwargs)

            destination = root / 'findings.pdf'
            with patch.object(fitz.Story, 'write_with_links', first_pass_uses_lossy_vendor_layout):
                render_pdf(markup, resources, destination)
            with fitz.open(destination) as document:
                rendered = text(document)
            sections = rendered.split('Compound18')
            self.assertEqual(len(sections), 2)
            for compound, section in zip(data['compounds'], sections):
                self.assertIn(re.sub(r'\s+', '', compound['original_text']), section)
                for warning in compound['all_warnings']:
                    label = warning.removeprefix('⚠️').removeprefix('❗').strip()
                    self.assertIn(re.sub(r'\s+', '', label), section)

    def test_missing_interior_text_with_every_heading_and_tail_retries_losslessly(self):
        original = fitz.Story.write_with_links
        calls = []

        def first_pass_loses_interior(story, *args, **kwargs):
            document = original(story, *args, **kwargs)
            calls.append(document.page_count)
            if len(calls) == 1:
                redact(document, 'A repeated diagnostic must appear once for every owner.', once=True)
            return document

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = findings()
            before = deepcopy(data)
            with patch.object(fitz.Story, 'write_with_links', first_pass_loses_interior):
                result = export_reports(configuration(root), data, log_callback=lambda _: None)
            self.assertFalse(result['partial'])
            self.assertGreater(len(calls), 1)
            with fitz.open(result['pdf_path']) as document:
                rendered = text(document)
            for number in (1, 2):
                self.assertIn(f'Ownedobservation{number}:interiorevidence.', rendered)
                self.assertIn(f'Ownedobservation{number}:finalevidence.', rendered)
            self.assertEqual(rendered.count('Arepeateddiagnosticmustappearonceforeveryowner.'), 2)
            self.assertEqual(data, before)

    def test_unrecoverable_interior_loss_is_partial_and_retains_exact_html_and_json(self):
        original = fitz.Story.write_with_links

        def always_loses_interior(story, *args, **kwargs):
            document = original(story, *args, **kwargs)
            redact(document, 'Owned observation 1: interior evidence.')
            return document

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = findings()
            before = deepcopy(data)
            with patch.object(fitz.Story, 'write_with_links', always_loses_interior):
                result = export_reports(configuration(root), data, log_callback=lambda _: None)
            self.assertTrue(result['partial'])
            self.assertEqual(result['status'], 'partial')
            self.assertIsNone(result['pdf_path'])
            self.assertIn('Owned observation 1: interior evidence.', Path(result['html_path']).read_text())
            saved = json.loads(Path(result['analysis_path']).read_text())
            self.assertEqual(saved['analysis'], before)
            self.assertEqual(saved['export']['status'], 'partial')
            self.assertEqual(data, before)

    def test_saved_pdf_is_rechecked_for_interior_content(self):
        original = fitz.Document.save

        def saved_pdf_loses_interior(document, *args, **kwargs):
            redact(document, 'Owned observation 1: interior evidence.')
            return original(document, *args, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(fitz.Document, 'save', saved_pdf_loses_interior):
                result = export_reports(configuration(Path(temporary)), findings(), log_callback=lambda _: None)
            self.assertTrue(result['partial'])
            self.assertIsNone(result['pdf_path'])
            self.assertTrue(result['artifact_warnings'])

    def test_missing_one_of_two_identical_structure_images_triggers_recovery(self):
        original = fitz.Story.write_with_links
        calls = []

        def first_pass_loses_one_image(story, *args, **kwargs):
            document = original(story, *args, **kwargs)
            calls.append(1)
            if len(calls) == 1:
                for page in document:
                    images = page.get_image_info()
                    if images:
                        page.add_redact_annot(fitz.Rect(images[0]['bbox']))
                        page.apply_redactions(images=1)
                        break
                self.assertEqual(sum(len(page.get_image_info()) for page in document), 1)
            return document

        with tempfile.TemporaryDirectory() as temporary:
            data = findings()
            for compound in data['compounds']:
                compound['smiles'] = 'CCO'
            with patch.object(fitz.Story, 'write_with_links', first_pass_loses_one_image):
                result = export_reports(configuration(Path(temporary)), data, log_callback=lambda _: None)
            self.assertFalse(result['partial'])
            self.assertGreater(len(calls), 1)
            with fitz.open(result['pdf_path']) as document:
                images = [image for page in document for image in page.get_image_info()]
            self.assertEqual(len(images), 2)

    def test_fallback_fragments_share_the_total_layout_budget(self):
        original = fitz.Story.write_with_links
        calls = []

        def many_pages_per_fragment(story, rectangle, *args, **kwargs):
            calls.append(1)
            for number in range(80):
                rectangle(number, fitz.Rect())
            document = original(story, rectangle, *args, **kwargs)
            if len(calls) == 1:
                redact(document, 'Owned observation 1: interior evidence.')
            return document

        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(fitz.Story, 'write_with_links', many_pages_per_fragment):
                result = export_reports(configuration(Path(temporary)), findings(), log_callback=lambda _: None)
            self.assertTrue(result['partial'])
            self.assertIsNone(result['pdf_path'])
            self.assertIn('page limit', ' '.join(result['artifact_warnings']))
            self.assertLessEqual(len(calls), 3)

    def test_all_sixty_distinct_findings_and_multipage_dp5_cells_survive(self):
        original = fitz.Story.write_with_links
        calls = []

        def first_pass_loses_twenty_four_rows(story, *args, **kwargs):
            document = original(story, *args, **kwargs)
            calls.append(1)
            if len(calls) == 1:
                for number in range(18, 42):
                    redact(document, f'Owned observation {number:02d}: unique analytical evidence.')
            return document

        data = findings(count=1, dp5_rows=110)
        compound = data['compounds'][0]
        compound['smiles'] = 'CCO'
        compound['all_warnings'] = [
            f'Owned observation {number:02d}: unique analytical evidence.' for number in range(60)]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            markup, resources = render_html(configuration(root), data, 'Summary',
                                           {'artifact_warnings': []}, {'name': 'source.pdf'})
            destination = root / 'findings.pdf'
            with patch.object(fitz.Story, 'write_with_links', first_pass_loses_twenty_four_rows):
                render_pdf(markup, resources, destination)
            self.assertGreater(len(calls), 1)
            with fitz.open(destination) as document:
                rendered = text(document)
                self.assertGreater(document.page_count, 3)
                self.assertTrue(any(page.get_images() for page in document))
            for number in range(60):
                self.assertIn(f'Ownedobservation{number:02d}:uniqueanalyticalevidence.', rendered)
            for index in range(110):
                self.assertIn(f'C1_{index:03d}', rendered)
            self.assertIn('DP5qoutcomes,fullsourcedescriptions', rendered)

    def test_table_fragments_keep_every_row_and_repeat_column_headers(self):
        original = fitz.Story.write_with_links
        calls = []

        def large_table_loses_an_interior_cell(story, *args, **kwargs):
            document = original(story, *args, **kwargs)
            rendered = text(document)
            calls.append(1)
            if 'C1_000' in rendered and 'C1_109' in rendered:
                redact(document, 'C1_055')
            return document

        with tempfile.TemporaryDirectory() as temporary:
            data = findings(count=1, dp5_rows=110)
            with patch.object(fitz.Story, 'write_with_links', large_table_loses_an_interior_cell):
                result = export_reports(configuration(Path(temporary)), data, log_callback=lambda _: None)
            self.assertFalse(result['partial'])
            self.assertGreater(len(calls), 3)
            with fitz.open(result['pdf_path']) as document:
                rendered = text(document)
            for index in range(110):
                self.assertIn(f'C1_{index:03d}', rendered)
            self.assertGreater(rendered.count('Assigned13Cshift'), 1)

    def test_isotope_calculated_percentage_warning_shows_formula_evidence(self):
        compound = {'all_warnings': ['ea_isotope_calcd_mismatch. Check isotope percentage convention.'],
                    'formula_sources': {'ea': {'candidate': {'raw': 'C6H5[13C]H3O'},
                                               'formula': 'C6H5[13C]H3O', 'status': 'valid'}}}
        self.assertIn('Formula source evidence', _formula_html(compound))
