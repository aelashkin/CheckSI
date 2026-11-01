"""Canonical metadata survives the actual runner, enrichment and report writers."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from docx import Document
from lxml import html
from openpyxl import load_workbook

from checksi.analysis import name_resolver
from checksi.application_runner import ApplicationRunner
from checksi.config_schema import ErrorSeverity
from checksi.dp5_enrichment import enrich_analysis_with_dp5
from checksi.dp5q.contracts import CheckResult
from checksi.errors import ErrorCode
from checksi.errors.findings import AnalysisNote, issue_counts, messages
from checksi.errors.manager import emit_finding
from checksi.errors.run_diagnostics import RunDiagnostics
from checksi.report_service import export_reports
from tests.test_controller import DESCRIPTION
from tests.test_hrms import make_config
from tests.test_name_resolver import StubWorker


class DiagnosticPipelineTests(unittest.TestCase):
    def test_repeated_enrichment_appends_occurrences_and_disabled_findings_keep_outcome(self):
        for severity in ('info', 'disabled'):
            with self.subTest(severity=severity):
                cfg = make_config(overrides={'dp5_not_applicable': severity})
                cfg.dp5.enabled = True
                run = RunDiagnostics(cfg)
                run.set_display(1, [])
                compound = {'id': 1, 'source_paragraph_1based': 2, 'smiles': 'CCO',
                            'run_dp5': True, 'dp5_c_signals_list': [18.2, 58.1],
                            'dp5_carbon_groups': [], 'all_warnings': []}
                data = {'compounds': [compound], 'error_stats': run.tracker.to_dict()}
                with patch('checksi.dp5_enrichment.DP5qSession') as session:
                    session.return_value.__enter__.return_value.check.return_value = CheckResult(
                        compound_id='1', status='not_applicable', reason='assignment_ambiguous',
                        diagnostics={'message': 'Ambiguous assignment.'})
                    for _ in range(2):
                        enrich_analysis_with_dp5(cfg, data, runtime_info={'model_id': 'fixture'},
                                                 _diagnostics=run)
                snapshot = run.snapshot()
                self.assertEqual(len(snapshot.occurrences), 2)
                self.assertIsNot(snapshot.occurrences[0], snapshot.occurrences[1])
                self.assertEqual(compound['dp5']['reason'], 'assignment_ambiguous')
                self.assertEqual(data['error_stats']['dp5_not_applicable'], 2 if severity == 'info' else 0)
                self.assertEqual(len(compound['all_warnings']), 2 if severity == 'info' else 0)

    def test_generated_document_reaches_enrichment_and_real_reports_with_one_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = Document()
            document.add_paragraph('ethanol (1)')
            document.add_paragraph(DESCRIPTION + ' HRMS (ESI): calcd for C2H7O+ [M+H]+ 47.0491, found 47.0501.')
            source = root / 'input.docx'
            document.save(source)
            cfg = make_config()
            cfg.paths.output_dir = root
            cfg.modes.deep_name_analysis = 'on'
            cfg.dp5.enabled = True
            snapshots = []
            def export(*args, **kwargs):
                snapshots.append(kwargs['_diagnostics'])
                return export_reports(*args, **kwargs)
            with (
                patch.object(name_resolver, '_ResolverWorker',
                             side_effect=lambda *args: StubWorker(lambda backend, name: 'CCO')),
                patch('checksi.application_runner.check_availability', return_value={'model_id': 'fixture'}),
                patch('checksi.dp5_enrichment.DP5qSession') as session,
                patch('checksi.application_runner.convert_docx_to_pdf', return_value=None),
                patch('checksi.application_runner.export_reports', side_effect=export),
            ):
                session.return_value.__enter__.return_value.check.return_value = CheckResult(
                    compound_id='1', status='not_applicable', reason='assignment_ambiguous',
                    diagnostics={'message': 'Ambiguous <assignment>.'})
                result = ApplicationRunner().execute(cfg, str(source))
            snapshot, = snapshots
            self.assertEqual(result.issue_counts, issue_counts(snapshot.occurrences))
            compound, = result.analysis_data['compounds']
            self.assertEqual(compound['all_warnings'], messages(snapshot.displays[1]))
            self.assertEqual(result.analysis_data['error_stats']['hrms_ppm_too_large'], 2)
            self.assertTrue(any(item.source.stage == 'dp5' for item in snapshot.occurrences))
            self.assertTrue(all(item.source.compound_id == 1 for item in snapshot.occurrences))
            saved = json.loads(Path(result.report_data['analysis_path']).read_text())
            self.assertEqual(saved['analysis'], result.analysis_data)
            self.assertNotIn('_diagnostics', saved['analysis'])
            readable = Path(result.report_data['html_path']).read_text()
            self.assertIn('Ambiguous &lt;assignment&gt;.', readable)
            workbook = load_workbook(result.report_data['error_stats_path'], read_only=True)
            try:
                rows = dict(workbook.active.iter_rows(min_row=2, values_only=True))
                self.assertEqual(rows, result.analysis_data['error_stats'])
            finally:
                workbook.close()

    def test_captured_report_severity_and_notes_survive_config_mutation_and_repeat_export(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = make_config(overrides={'name_not_found': 'warning'})
            cfg.paths.output_dir = Path(directory)
            run = RunDiagnostics(cfg)
            finding = emit_finding(cfg, run.sink(1, 2, 'name'), ErrorCode.NAME_NOT_FOUND)
            note = AnalysisNote('⚠️ name_not_found. Literal uncoded note')
            run.set_display(1, [finding, note])
            snapshot = run.snapshot()
            data = {'total_found': 1, 'error_stats': run.tracker.to_dict(), 'compounds': [{
                'id': 1, 'original_text': '<source>', 'source_paragraph_1based': 2,
                'all_warnings': messages(snapshot.displays[1]), 'highlight_info': {'color': 'yellow'},
            }]}
            before = deepcopy(data)
            cfg.errors.overrides[ErrorCode.NAME_NOT_FOUND] = ErrorSeverity.CRITICAL
            outputs = [export_reports(cfg, data, log_callback=lambda _: None, _diagnostics=snapshot)
                       for _ in range(2)]
            self.assertEqual(data, before)
            self.assertIs(snapshot.displays[1][0], finding)
            for output in outputs:
                doc = html.fromstring(Path(output['html_path']).read_text())
                coded = doc.xpath('//p[@class="warning" and contains(., "name_not_found.")]')
                self.assertEqual(len(coded), 1)
                literal, = doc.xpath('//p[contains(., "Literal uncoded note")]')
                self.assertEqual(literal.get('class'), 'diagnostic')
                payload = json.loads(Path(output['analysis_path']).read_text())
                self.assertEqual(payload['finding_severities']['name_not_found'], 'warning')
                self.assertEqual(payload['analysis'], before)
