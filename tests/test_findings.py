"""One captured occurrence supplies diagnostic projections and legacy adapters."""

from dataclasses import FrozenInstanceError
from decimal import Decimal
import threading
import unittest

from checksi.config_schema import ErrorSeverity
from checksi.errors import ErrorCode, emit_error
from checksi.errors.findings import (
    AnalysisNote, FindingSink, FindingSource, highlight_color, issue_counts, message, messages,
)
from checksi.errors.manager import emit_finding, format_finding
from checksi.errors.tracker import ErrorTracker, add_error
from tests.test_errors_config import make_config


class FindingTests(unittest.TestCase):
    def test_severity_nested_context_and_source_are_snapshots(self):
        cfg = make_config({'c13_count_structure_check': 'warning'})
        tracker = ErrorTracker()
        source = FindingSource(2, 7, 'nmr', '13C')
        context = {'annotations': {1: 1}, 'group_sizes': [2, 2]}
        finding = emit_finding(cfg, FindingSink(tracker, source),
                               ErrorCode.C13_COUNT_STRUCTURE_CHECK, context,
                               variant='annotation_partition')
        before = format_finding(finding)
        context['annotations'][1] = 8
        context['group_sizes'].append(9)
        finding.context['group_sizes'].append(10)
        cfg.errors.overrides[ErrorCode.C13_COUNT_STRUCTURE_CHECK] = ErrorSeverity.CRITICAL
        self.assertEqual(format_finding(finding), before)
        self.assertIn('{1: 1}', before)
        self.assertIn('[2, 2]', before)
        self.assertEqual(finding.severity, ErrorSeverity.WARNING)
        self.assertEqual(finding.source, source)
        self.assertEqual(finding.context['group_sizes'], [2, 2])
        with self.assertRaises(TypeError):
            finding.context['group_sizes'] = []
        with self.assertRaises(FrozenInstanceError):
            finding.source.compound_id = 9

    def test_repeated_occurrences_keep_order_and_all_projections(self):
        cfg = make_config({'hrms_ppm_too_large': 'warning'})
        tracker = ErrorTracker()
        first = emit_finding(cfg, tracker, ErrorCode.HRMS_PPM_TOO_LARGE,
                             {'ppm_diff': Decimal('21.252222')})
        second = emit_finding(cfg, tracker, ErrorCode.HRMS_PPM_TOO_LARGE,
                              {'calcd_mass': 47.0491, 'found_mass': 47.0501}, variant='values')
        third = emit_finding(cfg, tracker, ErrorCode.HRMS_PPM_TOO_LARGE,
                             {'ppm_diff': Decimal('21.252222')})
        self.assertEqual(tracker.occurrences, (first, second, third))
        self.assertIsNot(first, third)
        self.assertEqual(messages([third, second, first]),
                         [format_finding(third), format_finding(second), format_finding(first)])
        self.assertIn('21.3', format_finding(first))
        self.assertEqual(tracker.get_count('hrms_ppm_too_large'), 3)
        self.assertEqual(issue_counts(tracker.occurrences),
                         {'info': 0, 'warning': 3, 'critical': 0, 'anomaly_count': 3})
        self.assertEqual(highlight_color([first]), 'yellow')
        self.assertEqual(len(tracker.occurrences), 3)

    def test_disabled_occurrence_is_private_and_not_a_visible_or_counted_result(self):
        tracker = ErrorTracker()
        cfg = make_config({'formula_invalid_format': 'disabled'})
        self.assertIsNone(emit_finding(cfg, tracker, ErrorCode.FORMULA_INVALID_FORMAT))
        self.assertEqual(len(tracker.occurrences), 1)
        hidden, = tracker.occurrences
        self.assertEqual(hidden.severity, ErrorSeverity.DISABLED)
        self.assertEqual(messages([hidden]), [])
        self.assertIsNone(message(hidden))
        self.assertEqual(highlight_color([hidden]), 'blue')
        self.assertEqual(sum(tracker.to_dict().values()), 0)
        self.assertEqual(issue_counts(tracker.occurrences)['anomaly_count'], 0)

    def test_notes_request_color_without_acquiring_counts_or_severity(self):
        note = AnalysisNote('❗ Free diagnostic', 'yellow')
        self.assertEqual(messages([note]), ['❗ Free diagnostic'])
        self.assertEqual(highlight_color([note]), 'yellow')
        self.assertEqual(highlight_color([], fallback='yellow'), 'yellow')
        self.assertEqual(issue_counts([])['anomaly_count'], 0)

    def test_captured_critical_severity_takes_precedence_over_warnings_and_note_hints(self):
        cfg = make_config({'name_not_found': 'critical', 'hrms_ppm_too_large': 'warning'})
        tracker = ErrorTracker()
        critical = emit_finding(cfg, tracker, ErrorCode.NAME_NOT_FOUND)
        warning = emit_finding(cfg, tracker, ErrorCode.HRMS_PPM_TOO_LARGE, {'ppm_diff': 21})
        note = AnalysisNote('⚠️ Uncounted free text', 'yellow')
        cfg.errors.overrides[ErrorCode.NAME_NOT_FOUND] = ErrorSeverity.DISABLED
        for diagnostics in ([critical], [critical, warning, note], [note, warning, critical]):
            with self.subTest(diagnostics=diagnostics):
                self.assertEqual(highlight_color(diagnostics), 'red')
        self.assertEqual(highlight_color([warning, note]), 'yellow')
        self.assertEqual(highlight_color([AnalysisNote('⚠️ Uncounted free text')]), 'blue')

    def test_legacy_mutations_and_emission_do_not_count_twice(self):
        tracker = ErrorTracker()
        tracker.add('name_not_found')
        add_error(tracker, 'name_not_found')
        rendered = emit_error(make_config(), tracker, ErrorCode.NAME_NOT_FOUND)
        self.assertIs(type(rendered), str)
        self.assertEqual(tracker.get_count('name_not_found'), 3)
        self.assertEqual(len(tracker.occurrences), 1)
        copied = tracker.to_dict()
        copied['name_not_found'] = 99
        self.assertEqual(tracker.get_count('name_not_found'), 3)
        for name in ('unknown', 'nmr_solvent_and_frequency_missing'):
            with self.assertRaises(ValueError):
                tracker.add(name)
        with self.assertRaises(TypeError):
            emit_error(make_config(), tracker, 'name_not_found')

    def test_template_fallback_and_missing_values_keep_legacy_bytes(self):
        tracker = ErrorTracker()
        cfg = make_config({'hrms_ppm_too_large': 'info'})
        missing = emit_error(cfg, tracker, ErrorCode.HRMS_PPM_TOO_LARGE, variant='unknown')
        self.assertIn('{ppm_diff:.1f}', missing)
        failure = emit_error(cfg, tracker, ErrorCode.HRMS_PPM_TOO_LARGE, {'ppm_diff': 'bad'})
        self.assertIn('{ppm_diff:.1f}', failure)

    def test_uncopyable_legacy_context_keeps_raw_template_fallback_and_disabled_return(self):
        from checksi.errors.registry import ERROR_TEMPLATES
        for severity in ('info', 'disabled'):
            with self.subTest(severity=severity):
                tracker = ErrorTracker()
                cfg = make_config({'hrms_ppm_too_large': severity})
                result = emit_error(cfg, tracker, ErrorCode.HRMS_PPM_TOO_LARGE,
                                    {'ppm_diff': threading.Lock()})
                if severity == 'disabled':
                    self.assertIsNone(result)
                else:
                    self.assertEqual(result, 'hrms_ppm_too_large. ' +
                                     ERROR_TEMPLATES[ErrorCode.HRMS_PPM_TOO_LARGE]['default'])
                self.assertEqual(tracker.get_count('hrms_ppm_too_large'), int(severity != 'disabled'))
                self.assertEqual(tracker.occurrences, ())
