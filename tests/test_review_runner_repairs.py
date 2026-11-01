"""Completed findings survive optional source conversion and cancellation."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fitz
from docx import Document

from checksi import main
from checksi.application_runner import ApplicationRunner
from checksi.config import ConfigManager
from checksi.events import ProgressEvent
from checksi.utils.platform_helpers import ConversionCancelled
from tests.test_review_report_repairs import make_config


class ReviewRunnerRepairs(unittest.TestCase):
    def test_docx_conversion_success_missing_and_cancelled_keep_findings(self):
        for outcome in ('success', 'missing', 'cancelled'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                cfg = make_config(root)
                path = root / 'Logical δ .docx directory' / 'article.DOCX'
                path.parent.mkdir()
                document = Document()
                document.add_paragraph('General methods.')
                document.save(path)
                cfg.paths.input_path = path
                cfg.modes.open_reports_on_success = True
                events = []
                def convert(source, log, *, work_dir, **kwargs):
                    self.assertEqual(Path(source), path)
                    self.assertTrue(any(isinstance(e, ProgressEvent) and e.message == 'DOCX source conversion' for e in events))
                    self.assertTrue(Path(work_dir).is_dir())
                    if outcome == 'cancelled':
                        kwargs['cancel_token'].set()
                        raise ConversionCancelled('cancelled fixture')
                    if outcome == 'missing':
                        log('LibreOffice missing fixture')
                        return None
                    target = Path(work_dir) / 'article.pdf'
                    with fitz.open() as pdf:
                        pdf.new_page().insert_text((40, 40), 'General methods.')
                        pdf.save(target)
                    return target
                import threading
                token = threading.Event()
                with patch('checksi.application_runner.convert_docx_to_pdf', side_effect=convert), patch('checksi.application_runner.open_system_file') as opened:
                    result = ApplicationRunner(event_dispatcher=events.append).execute(cfg, str(path), token)
                for key in ('html_path','analysis_path','error_stats_path'):
                    self.assertTrue(Path(result.report_data[key]).is_file())
                self.assertEqual(result.analysis_data['processed_paragraphs'], 1)
                self.assertEqual(result.report_data['aborted'], outcome == 'cancelled')
                self.assertEqual(result.report_data['partial'], outcome != 'success')
                payload = json.loads(Path(result.report_data['analysis_path']).read_text(encoding="utf-8"))
                self.assertEqual(payload['export']['status'], 'cancelled' if outcome == 'cancelled' else 'partial' if outcome == 'missing' else 'complete')
                if outcome == 'cancelled':
                    opened.assert_not_called()
                    self.assertIsNone(result.report_data['pdf_path'])
                    self.assertIn('conversion cancelled', ' '.join(result.report_data['artifact_warnings']))
                self.assertEqual(Path(result.report_data['output_dir']).parent, cfg.paths.output_dir)

    def test_runner_exception_removes_only_its_empty_reservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root)
            cfg.paths.output_dir.mkdir()
            sentinel = cfg.paths.output_dir / 'previous.txt'
            sentinel.write_text('keep')
            with patch('checksi.document_parser.paragraphs_from_input', side_effect=OSError('fixture unreadable')):
                with self.assertRaisesRegex(OSError, 'fixture unreadable'):
                    ApplicationRunner(log_callback=lambda _: None).execute(cfg, str(cfg.paths.input_path))
            self.assertEqual(list(cfg.paths.output_dir.iterdir()), [sentinel])

    def test_cli_cancellation_precedes_partial_and_findings_with_real_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root)
            path = root / 'settings.toml'
            manager = ConfigManager(path)
            manager.save(cfg)
            source = root / 'article.DOCX'
            document = Document()
            document.add_paragraph('General methods.')
            document.save(source)
            args = main._build_parser().parse_args(['run', str(source)])
            def convert(*args, **kwargs):
                kwargs['cancel_token'].set()
                raise ConversionCancelled('fixture')
            with patch('checksi.application_runner.convert_docx_to_pdf', side_effect=convert), contextlib.redirect_stderr(io.StringIO()):
                logs = []
                self.assertEqual(main._handle_run(args, manager, log_callback=logs.append), 130)
            self.assertTrue(any('export_status: cancelled' in line for line in logs))
            self.assertTrue(any('artifact_warning:' in line for line in logs))
            self.assertTrue(any('results_json:' in line and 'None' not in line for line in logs))

    def test_complete_artifacts_with_unmatched_source_coverage_keep_finding_exit_two(self):
        from checksi.report_rendering import font_resources
        from tests.test_chemistry_engine import NMR
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root)
            text = NMR + 'HRMS (ESI) calcd for C2H7O+ [M+H]+ 47.0491, found 47.0501'
            with fitz.open() as source:
                page = source.new_page()
                page.insert_font(fontname='Fixture', fontbuffer=font_resources()['NotoSans-Regular.ttf'])
                self.assertGreaterEqual(page.insert_textbox(fitz.Rect(72,72,520,720), text, fontname='Fixture', fontsize=11), 0)
                source.save(cfg.paths.input_path)
            manager = ConfigManager(root/'settings.toml')
            manager.save(cfg)
            args = main._build_parser().parse_args(['run', str(cfg.paths.input_path)])
            logs = []
            # The source matcher is replaced so the description cannot be
            # located; this test covers the runner's exit and export contract.
            with contextlib.redirect_stderr(io.StringIO()), \
                    patch('checksi.source_annotations._description_occurrences', return_value=[]):
                self.assertEqual(main._handle_run(args, manager, log_callback=logs.append), 2)
            self.assertIn('export_status: complete', logs)
            self.assertTrue(any(line.startswith('annotation_coverage:') for line in logs))
            payload = json.loads(next(cfg.paths.output_dir.glob('*/results.json')).read_text(encoding="utf-8"))
            self.assertEqual(payload['analysis']['total_found'], 1)
            self.assertTrue(payload['annotation_warnings'])
            self.assertFalse(payload['export']['partial'])
            for key in ('html_path','pdf_path','analysis_path','source_pdf_path','error_stats_path'):
                self.assertTrue(Path(payload['export'][key]).is_file())
