"""Report exports own annotations without changing supplied analysis results."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fitz
import openpyxl

from checksi.config_schema import AppConfig
from checksi.report_service import export_reports
from checksi.source_annotations import annotate_source
from checksi.utils.error_handler import ERROR_NAMES


def make_config(directory: str) -> AppConfig:
    """Keep all report side effects inside a temporary directory."""
    return AppConfig.model_validate({
        "paths": {
            "input_path": str(Path(directory) / "input.pdf"),
            "output_dir": directory,
        },
        "modes": {"deep_name_analysis": "off", "output_mode": "detailed"},
        "solvents": {"entries": []},
        "errors": {"default_severity": "critical", "overrides": {"dp5_analysis_failed": "warning", "stereo_check_required": "info"}},
    })


def make_analysis() -> dict:
    """Include shared warning lists, duplicate phrases and unsorted DP5 atoms."""
    warnings = ["⚠️ name_not_found. Check the reported shifts.", "❗ dp5_analysis_failed. Check the assignment.", "stereo_check_required. Informational note."]
    return {
        "total_found": 2,
        "error_stats": {ERROR_NAMES[0]: 2, ERROR_NAMES[-1]: 1},
        "compounds": [{
            "id": number,
            "name": f"Compound {number}",
            "identifier": str(number),
            "original_text": f"Compound {number} Shared proton data Carbon data {number}",
            "all_warnings": warnings,
            "highlight_info": {
                "color": "red", "name_text": f"Compound {number}",
                "hnmr_text": "Shared proton data", "cnmr_text": f"Carbon data {number}",
            },
            "dp5": {
                "status": "success", "probability_percent": 5.0,
                "atoms": [{"label": "C_low", "assigned_shift": 20.0},
                          {"label": "C_high", "assigned_shift": 60.0}],
            },
        } for number in (1, 2)],
    }


def write_pdf(path: Path) -> None:
    """Create a small searchable PDF without external document conversion."""
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), "Compound 1\nShared proton data\nCarbon data 1\nCompound 2\nShared proton data\nCarbon data 2")
        document.save(path)


def spreadsheet_content(path: str) -> list:
    """Read saved statistics in their canonical row order."""
    workbook = openpyxl.load_workbook(path)
    try:
        return list(workbook.active.values)
    finally:
        workbook.close()


def pdf_annotations(path: str) -> list[tuple]:
    """Associate each saved highlight with its source text and comment."""
    with fitz.open(path) as document:
        return [(page.get_textbox(fitz.Quad(annot.vertices[:4]).rect).strip(),
                 annot.info["title"], annot.info["content"], annot.colors["stroke"])
                for page in document for annot in page.annots() if annot.type[0] == 8]


class ReportIsolationTests(unittest.TestCase):
    """Real portable writers retain analysis and keep each export independent."""

    def test_repeated_exports_preserve_analysis_and_semantic_outputs(self):
        import json
        with tempfile.TemporaryDirectory() as directory:
            cfg, analysis = make_config(directory), make_analysis()
            before = deepcopy(analysis)
            outputs = [export_reports(cfg, analysis, log_callback=lambda _: None) for _ in range(2)]
            self.assertEqual(analysis, before)
            self.assertNotEqual(outputs[0]['output_dir'], outputs[1]['output_dir'])
            for output in outputs:
                saved = json.loads(Path(output['analysis_path']).read_text(encoding="utf-8"))
                self.assertEqual(saved['analysis'], before)
                text = Path(output['html_path']).read_text(encoding="utf-8")
                self.assertIn('Critical:', text)
                self.assertIn('Warning:', text)
                self.assertIn('Info:', text)
                self.assertEqual(text.count('C_high'), 2)
                self.assertLess(text.index('C_high'), text.index('C_low'))
            self.assertEqual(spreadsheet_content(outputs[0]['error_stats_path']),
                             spreadsheet_content(outputs[1]['error_stats_path']))
            rows = spreadsheet_content(outputs[0]['error_stats_path'])
            self.assertEqual(rows[0], ('ERROR', 'input.pdf'))
            self.assertEqual([row[0] for row in rows[1:]], ERROR_NAMES)
            self.assertEqual([row[1] for row in rows[1:]], [before['error_stats'].get(name, 0) for name in ERROR_NAMES])

    def test_write_failures_preserve_analysis_and_retry_without_overwriting(self):
        for boundary in ('fitz.Document.save', 'openpyxl.Workbook.save'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                cfg, analysis = make_config(directory), make_analysis()
                pdf_path = cfg.paths.input_path
                write_pdf(pdf_path)
                before = deepcopy(analysis)
                originals = {Path(directory) / name: b'pre-existing user artifact'
                             for name in ('findings.html', 'findings.pdf', 'statistics.xlsx')}
                for path, content in originals.items():
                    path.write_bytes(content)
                with patch(boundary, side_effect=OSError('controlled write failure')):
                    result = export_reports(cfg, analysis, pdf_path=str(pdf_path), log_callback=lambda _: None)
                self.assertTrue(result['partial'])
                self.assertEqual(analysis, before)
                retry = export_reports(cfg, analysis, pdf_path=str(pdf_path), log_callback=lambda _: None)
                self.assertFalse(retry['partial'])
                for key in ('html_path', 'analysis_path', 'pdf_path', 'source_pdf_path', 'error_stats_path'):
                    self.assertTrue(Path(retry[key]).is_file())
                    self.assertNotIn(Path(retry[key]), originals)
                for path, content in originals.items():
                    self.assertEqual(path.read_bytes(), content)

    def test_nested_export_cannot_replace_outer_annotation_ownership(self):
        with tempfile.TemporaryDirectory() as outer_dir, tempfile.TemporaryDirectory() as inner_dir:
            outer_cfg, inner_cfg = make_config(outer_dir), make_config(inner_dir)
            outer, inner = make_analysis(), make_analysis()
            before = deepcopy((outer, inner))
            write_pdf(outer_cfg.paths.input_path)
            write_pdf(inner_cfg.paths.input_path)
            calls = []
            def annotate(source, target, compounds, summary, **options):
                calls.append(compounds)
                if source == str(outer_cfg.paths.input_path):
                    export_reports(inner_cfg, inner, pdf_path=str(inner_cfg.paths.input_path), log_callback=lambda _: None)
                return annotate_source(source, target, compounds, summary, **options)
            with patch('checksi.report_service.annotate_source', side_effect=annotate):
                result = export_reports(outer_cfg, outer, pdf_path=str(outer_cfg.paths.input_path), log_callback=lambda _: None)
            self.assertEqual(len(calls), 2)
            self.assertIsNot(calls[0], calls[1])
            self.assertEqual(result['highlighted_compounds'], 2)
            self.assertEqual((outer, inner), before)

    def test_highlight_count_uses_unique_compound_ids_not_comment_strings(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg, analysis = make_config(directory), make_analysis()
            write_pdf(cfg.paths.input_path)
            before = deepcopy(analysis)
            outputs = [export_reports(cfg, analysis, pdf_path=str(cfg.paths.input_path), log_callback=lambda _: None) for _ in range(2)]
            self.assertEqual(analysis, before)
            self.assertEqual([item['highlighted_compounds'] for item in outputs], [2, 2])
            self.assertEqual(pdf_annotations(outputs[0]['source_pdf_path']), pdf_annotations(outputs[1]['source_pdf_path']))
            ids = {item['compound_id'] for item in outputs[0]['source_associations']}
            self.assertEqual(ids, {1, 2})
            expected = '\n'.join(before['compounds'][0]['all_warnings'])
            notes = [content for _, _, content, _ in pdf_annotations(outputs[0]['source_pdf_path']) if content]
            self.assertEqual(notes, ['Compound 1.\n' + expected, 'Compound 2.\n' + expected])

    def test_export_routes_each_compounds_own_color_and_associations(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = make_config(directory)
            analysis = {'total_found': 3, 'error_stats': {}, 'compounds': []}
            for number, color in enumerate(('red', 'yellow', 'blue'), 1):
                analysis['compounds'].append({'id': number, 'original_text': f'{color} name {color} carbon Priority phrase', 'all_warnings': [f'{color} warning'],
                    'highlight_info': {'color': color, 'name_text': f'{color} name', 'hnmr_text': 'Priority phrase', 'cnmr_text': f'{color} carbon'}})
            before = deepcopy(analysis)
            with fitz.open() as source:
                source.new_page().insert_text((72, 72), 'red name\nred carbon\nPriority phrase\nyellow name\nyellow carbon\nPriority phrase\nblue name\nblue carbon\nPriority phrase')
                source.save(cfg.paths.input_path)
            result = export_reports(cfg, analysis, pdf_path=str(cfg.paths.input_path), log_callback=lambda _: None)
            self.assertEqual(analysis, before)
            phrases = {text: (title, content, color) for text, title, content, color in pdf_annotations(result['source_pdf_path']) if title != 'stat'}
            shared = [a for a in result['source_associations'] if a['phrase'] == 'Priority phrase']
            self.assertEqual({(a['compound_id'], a['color']) for a in shared}, {(1,'red'), (2,'yellow'), (3,'blue')})
            for color, stroke in (('red', [1., 0., 0.]), ('yellow', [1., 1., 0.]), ('blue', [.68, .85, .9])):
                for actual, expected in zip(phrases[f'{color} name'][2], stroke):
                    self.assertAlmostEqual(actual, expected, places=6)
            self.assertEqual(result['highlighted_compounds'], 3)

    def test_failure_inside_rendering_retains_machine_readable_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg, analysis = make_config(directory), make_analysis()
            analysis['compounds'][1]['dp5']['probability_percent'] = 'invalid probability'
            before = deepcopy(analysis)
            with self.assertRaises(ValueError):
                export_reports(cfg, analysis, log_callback=lambda _: None)
            self.assertEqual(analysis, before)
            self.assertEqual(len(list(Path(directory).glob('*/results.json'))), 1)

    def test_filename_is_literal_spreadsheet_text(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = make_config(directory)
            cfg.paths.input_path = Path(directory) / '=1+1.pdf'
            result = export_reports(cfg, make_analysis(), log_callback=lambda _: None)
            workbook = openpyxl.load_workbook(result['error_stats_path'])
            try:
                self.assertEqual(workbook.active['B1'].value, '=1+1.pdf')
                self.assertEqual(workbook.active['B1'].data_type, 's')
            finally:
                workbook.close()
