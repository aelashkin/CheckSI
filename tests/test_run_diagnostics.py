"""The live controller projects one per-run occurrence owner into legacy data."""

import unittest
from unittest.mock import patch

from checksi.analysis import name_resolver
from checksi.controller import CheckSIController
from checksi.errors import ErrorCode
from checksi.errors.findings import Finding, messages
from checksi.errors.run_diagnostics import RunDiagnostics
from tests.test_controller import DESCRIPTION
from tests.test_name_resolver import StubWorker, make_config


class RunDiagnosticTests(unittest.TestCase):
    def test_new_carbon_inventory_finding_uses_pending_canonical_source_at_every_severity(self):
        text = ('1H NMR (400 MHz, CDCl3): δ 3.60 (s, 6H). '
                '13C NMR (100 MHz, CDCl3): δ 60.0 (4C), 20.0. '
                'HRMS (ESI): calcd for C2H7O+ [M+H]+ 47.0491, found 47.0491.')
        for severity in ('disabled', 'info', 'warning', 'critical'):
            with self.subTest(severity=severity):
                cfg = make_config({'c13_count_exceeds_formula': severity})
                cfg.modes.deep_name_analysis = 'off'
                cfg.errors.default_severity = 'disabled'
                run = RunDiagnostics(cfg)
                result = CheckSIController(cfg, event_dispatcher=lambda _: None).run_analysis(
                    [text], _diagnostics=run)
                snapshot = run.snapshot()
                finding, = [item for item in snapshot.occurrences
                            if item.code is ErrorCode.C13_COUNT_EXCEEDS_FORMULA]
                self.assertEqual(finding.severity.value, severity)
                self.assertEqual((finding.source.compound_id, finding.source.paragraph_1based,
                                  finding.source.stage, finding.source.nucleus), (1, 1, 'nmr', '13C'))
                self.assertEqual(finding.context['nmr_count'], 5)
                self.assertEqual(result['error_stats']['c13_count_exceeds_formula'], int(severity != 'disabled'))
                compound, = result['compounds']
                self.assertFalse(compound['run_dp5'])
                self.assertEqual(compound['all_warnings'], messages(snapshot.displays[1]))

    def test_real_controller_keeps_source_and_display_references_per_compound(self):
        cfg = make_config()
        run = RunDiagnostics(cfg)
        def worker(*args):
            return StubWorker(lambda backend, name: 'CCO')
        with patch.object(name_resolver, '_ResolverWorker', side_effect=worker):
            data = CheckSIController(cfg).run_analysis(
                ['ethanol (1)', DESCRIPTION, 'ethanol (2)', DESCRIPTION], _diagnostics=run)
        snapshot = run.snapshot()
        self.assertTrue(snapshot.occurrences)
        self.assertEqual(data['error_stats'], run.tracker.to_dict())
        for compound in data['compounds']:
            display = snapshot.displays[compound['id']]
            self.assertEqual(compound['all_warnings'], messages(display))
            own = [item for item in display if isinstance(item, Finding)]
            self.assertTrue(own)
            self.assertTrue(all(item.source.compound_id == compound['id'] for item in own))
            self.assertTrue(all(item.source.paragraph_1based == compound['source_paragraph_1based']
                                for item in own if item.source.stage == 'nmr'))
        with self.assertRaises(TypeError):
            snapshot.displays[1] = ()
        empty = CheckSIController(cfg).run_analysis([])
        self.assertFalse(any(empty['error_stats'].values()))

    def test_finalization_checks_visible_handles_and_filters_hidden_occurrences(self):
        from checksi.errors.manager import emit_finding
        cfg = make_config({'name_not_found': 'disabled'})
        run = RunDiagnostics(cfg)
        self.assertIsNone(emit_finding(cfg, run.sink(1, 2, 'name'), ErrorCode.NAME_NOT_FOUND))
        run.set_display(1, [])
        self.assertEqual(len(run.snapshot().occurrences), 1)
        visible = emit_finding(cfg, run.sink(1, 2, 'nmr', '13C'), ErrorCode.C13_SECTION_MISSING)
        with self.assertRaises(ValueError):
            run.snapshot()
        run.set_display(1, [visible])
        self.assertIs(run.snapshot().displays[1][0], visible)
        run.set_display(1, [visible, visible])
        with self.assertRaises(ValueError):
            run.snapshot()

    def test_finalization_rejects_a_finding_under_another_compounds_display(self):
        from checksi.errors.manager import emit_finding
        cfg = make_config()
        run = RunDiagnostics(cfg)
        finding = emit_finding(cfg, run.sink(1, 2, 'name'), ErrorCode.NAME_NOT_FOUND)
        run.set_display(2, [finding])
        with self.assertRaisesRegex(ValueError, 'ownership'):
            run.snapshot()

    def test_finalization_rejects_visible_unbound_source_in_canonical_displays(self):
        from checksi.errors.manager import emit_finding
        cfg = make_config()
        run = RunDiagnostics(cfg)
        finding = emit_finding(cfg, run.tracker, ErrorCode.NAME_NOT_FOUND)
        run.set_display(1, [finding])
        with self.assertRaisesRegex(ValueError, 'ownership'):
            run.snapshot()

    def test_pending_compound_commits_original_references_once_with_separate_display_order(self):
        from checksi.errors.manager import emit_finding
        cfg = make_config()
        run = RunDiagnostics(cfg)
        pending = run.begin_compound(1)
        first = emit_finding(cfg, pending.sink(2, 'name'), ErrorCode.NAME_NOT_FOUND)
        second = emit_finding(cfg, pending.sink(2, 'nmr', '13C'), ErrorCode.C13_SECTION_MISSING)
        self.assertEqual(run.tracker.occurrences, ())
        pending.commit([second, first])
        snapshot = run.snapshot()
        self.assertEqual(snapshot.occurrences, (first, second))
        self.assertEqual(snapshot.displays[1], (second, first))
        with self.assertRaises(RuntimeError):
            pending.commit([second, first])
        with self.assertRaises(RuntimeError):
            emit_finding(cfg, pending.sink(2, 'name'), ErrorCode.NAME_NOT_FOUND)
        discarded = run.begin_compound(2)
        emit_finding(cfg, discarded.sink(4, 'name'), ErrorCode.NAME_NOT_FOUND)
        self.assertEqual(run.snapshot().occurrences, (first, second))
        self.assertEqual(set(run.snapshot().displays), {1})

    def test_pending_invalid_display_cannot_partially_publish_occurrences(self):
        from checksi.errors.manager import emit_finding
        cfg = make_config()
        run = RunDiagnostics(cfg)
        pending = run.begin_compound(1)
        finding = emit_finding(cfg, pending.sink(2, 'name'), ErrorCode.NAME_NOT_FOUND)
        with self.assertRaises(ValueError):
            pending.commit([])
        self.assertEqual(run.tracker.occurrences, ())
        self.assertEqual(dict(run.snapshot().displays), {})
        pending.commit([finding])
        self.assertIs(run.snapshot().occurrences[0], finding)
