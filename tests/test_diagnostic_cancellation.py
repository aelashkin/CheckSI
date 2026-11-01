"""Late resolver cancellation publishes only completed compound diagnostics."""

import contextlib
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from docx import Document

from checksi import main
from checksi.analysis import name_resolver
from checksi.config import ConfigManager
from checksi.controller import CheckSIController
from checksi.errors.findings import messages
from checksi.errors.run_diagnostics import RunDiagnostics
from tests.test_name_resolver import StubWorker, make_config


DESCRIPTION = (
    '1H NMR (400 MHz, CDCl3): δ 3.60 (q, J = 7.2 Hz, 2H), 2.00 (s, 1H), '
    '1.20 (t, J = 7.2 Hz, 3H). 13C NMR (100 MHz, CDCl3): δ 60.0, 20.0. '
    'HRMS (ESI): calcd for C2H7O+ [M+H]+ 47.0491, found 47.0491.'
)


def config(severity):
    cfg = make_config({'name_modified_for_smiles': severity})
    cfg.errors.default_severity = 'disabled'
    return cfg


class ObservedWorker(StubWorker):
    closed = False

    def close(self):
        self.closed = True
        return super().close()


def worker(*args, created=None):
    instance = ObservedWorker(lambda backend, name: 'CCO' if name == 'ethanol' else None)
    if created is not None:
        created.append(instance)
    return instance


def paragraphs(completed):
    return ((['Synthesis of ethanol (1)', DESCRIPTION] if completed else []) +
            ['Synthesis of ethanol (2)', DESCRIPTION])


def cancel_after_real_resolver(*args, **kwargs):
    result = name_resolver._smiles_and_name_from_paragraph(*args, **kwargs)
    if args[2].endswith('(2)'):
        kwargs['cancel_token'].set()
    return result


class DiagnosticCancellationTests(unittest.TestCase):
    def test_late_cancel_keeps_completed_compounds_and_discards_pending_findings(self):
        for completed in (0, 1):
            for severity in ('disabled', 'info', 'warning', 'critical'):
                with self.subTest(completed=completed, severity=severity):
                    cfg = config(severity)
                    run = RunDiagnostics(cfg)
                    token = threading.Event()
                    workers = []
                    with (
                        patch.object(name_resolver, '_ResolverWorker',
                                     side_effect=lambda *args: worker(*args, created=workers)),
                        patch('checksi.controller._smiles_and_name_from_paragraph',
                              side_effect=cancel_after_real_resolver),
                    ):
                        baseline = (CheckSIController(cfg, event_dispatcher=lambda _: None).run_analysis(
                            paragraphs(completed)[:2]) if completed else None)
                        result = CheckSIController(cfg, event_dispatcher=lambda _: None).run_analysis(
                            paragraphs(completed), cancel_token=token, _diagnostics=run)
                    self.assertTrue(result['aborted'])
                    self.assertTrue(workers)
                    self.assertTrue(all(item.closed for item in workers))
                    self.assertEqual(result['total_found'], completed)
                    self.assertEqual([item['id'] for item in result['compounds']], [1] if completed else [])
                    snapshot = run.snapshot()
                    self.assertEqual(set(snapshot.displays), {1} if completed else set())
                    self.assertEqual(result['error_stats']['name_modified_for_smiles'],
                                     completed if severity != 'disabled' else 0)
                    self.assertEqual(len(snapshot.occurrences), completed)
                    self.assertEqual(result['name_finding_stats']['numeric_id_match_count'], completed)
                    if baseline is not None:
                        self.assertEqual(result['compounds'], baseline['compounds'])
                        self.assertEqual(result['error_stats'], baseline['error_stats'])
                    for compound in result['compounds']:
                        self.assertEqual(compound['all_warnings'], messages(snapshot.displays[1]))
                        self.assertEqual(compound['name_resolution']['status'], 'transformed')
                        self.assertEqual(compound['name_resolution']['accepted_name'], 'ethanol')
                        self.assertEqual(set(compound['name_resolution']), {
                            'status', 'reason', 'original_name', 'accepted_name',
                            'identifier', 'transformations'})
                    self.assertTrue(all(item.source.compound_id == 1 for item in snapshot.occurrences))

    def test_actual_cli_returns_130_after_late_resolver_cancel_at_all_severities(self):
        for completed in (0, 1):
            for severity in ('disabled', 'info', 'warning', 'critical'):
                with self.subTest(completed=completed, severity=severity), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    source = root / 'input.docx'
                    document = Document()
                    for text in paragraphs(completed):
                        document.add_paragraph(text)
                    document.save(source)
                    cfg = config(severity)
                    cfg.paths.output_dir = root / 'reports'
                    manager = ConfigManager(root / 'settings.toml')
                    manager.save(cfg)
                    args = main._build_parser().parse_args(['run', str(source)])
                    output, progress = io.StringIO(), io.StringIO()
                    with (
                        patch.object(name_resolver, '_ResolverWorker', side_effect=worker),
                        patch('checksi.controller._smiles_and_name_from_paragraph',
                              side_effect=cancel_after_real_resolver),
                        contextlib.redirect_stderr(progress),
                    ):
                        code = main._handle_run(args, manager, log_callback=lambda text: print(text, file=output))
                    self.assertEqual(code, 130, output.getvalue())
                    self.assertIn(f'compounds_detected: {completed}', output.getvalue())
                    for label, level in (('critical_issues', 'critical'),
                                         ('warning_issues', 'warning'),
                                         ('informational_notes', 'info')):
                        count = completed if severity == level else 0
                        self.assertIn(f'{label}: {count}\n', output.getvalue())
                    self.assertIn('report_html: not generated', output.getvalue())
                    self.assertIn('Run cancelled', output.getvalue())
                    # Preflight keeps the output parent but releases its empty run reservation.
                    self.assertEqual(list(cfg.paths.output_dir.iterdir()), [])
