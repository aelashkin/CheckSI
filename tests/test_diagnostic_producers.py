"""Real producers retain canonical handles until their public string adapters."""

import unittest
from unittest.mock import patch

from checksi.analysis.elemental import _check_elemental_analysis, check_elemental_analysis
from checksi.analysis.hrms import _check_hrms, check_hrms
from checksi.analysis.nmr import _check_nmr, check_nmr, CheckContext, CARBON
from checksi.analysis.stereochem import _check_all_same_type, check_all_same_type
from checksi.analysis import name_resolver
from checksi.errors import ErrorCode
from checksi.errors.findings import AnalysisNote, Finding, FindingSink, FindingSource, messages
from checksi.errors.tracker import ErrorTracker
from tests.test_hrms import make_config
from tests.test_name_resolver import StubWorker, make_config as resolver_config


class DiagnosticProducerTests(unittest.TestCase):
    def test_no_structure_carbon_notes_keep_explicit_yellow_hints(self):
        tracker = ErrorTracker()
        checked = _check_nmr('13C NMR (100 MHz, CDCl3): δ 60.0, 20.0.',
            CheckContext(make_config(), tracker, CARBON, trees_count=None, frequency_h=400,
                atom_with_four_unique_substituents=0, atom_with_two_identical_substituents=0,
                formula_nmr='C2H6O', e_formula=0, onh_count=0, c13_count_alternatives=None,
                carbon_groups=None, c13_count_alternatives_w_groups=None, stereogenic_signal_flag=False))
        yellow_notes = [item for item in checked.diagnostics
                        if isinstance(item, AnalysisNote) and item.highlight_hint == 'yellow']
        self.assertEqual(len(yellow_notes), 2)
        self.assertTrue(yellow_notes[0].text.startswith('❗ C NMR Result:'))
        self.assertTrue(yellow_notes[1].text.startswith('❗ Check equivalent  C:'))
        self.assertEqual(sum(tracker.to_dict().values()), 0)

    def test_nmr_keeps_emission_order_separate_from_display_order(self):
        text = '13C NMR (100 MHz, CDCl3): δ 20.0, 60.0, 20.0, 5, 7.12.'
        tracker = ErrorTracker()
        checked = _check_nmr(text,
            CheckContext(make_config(), tracker, CARBON, trees_count=None, frequency_h=400,
                atom_with_four_unique_substituents=0, atom_with_two_identical_substituents=0,
                formula_nmr=None, e_formula=0, onh_count=0, c13_count_alternatives=None,
                carbon_groups=None, c13_count_alternatives_w_groups=None, stereogenic_signal_flag=False))
        expected = [ErrorCode.NMR_SIGNAL_ORDER_INVALID, ErrorCode.NMR_SIGNAL_PRECISION_INVALID,
                    ErrorCode.NMR_SIGNAL_ROUNDING_NEEDED, ErrorCode.XNUC_DUPLICATE_SIGNALS]
        emitted = [item for item in tracker.occurrences if item.code in expected]
        displayed = [item for item in checked.diagnostics if isinstance(item, Finding) and item.code in expected]
        self.assertEqual([item.code for item in emitted], expected)
        self.assertEqual(displayed, [emitted[0], emitted[2], emitted[3], emitted[1]])
        public = check_nmr(make_config(), ErrorTracker(), text, '13C', 1, None, 400,
                           0, 0, None, 0, 0)
        self.assertEqual(public, (messages(checked.diagnostics), checked.frequency, checked.ratio, checked.section,
                                  checked.eq_c_ratio, checked.preparation.groups,
                                  checked.preparation.eligible, checked.preparation.shifts))

    def test_stereo_single_and_list_adapters_retain_the_corrected_contract(self):
        tracker = ErrorTracker()
        homogeneous, kind, diagnostics = _check_all_same_type(make_config(), tracker, ['(R,S)'])
        self.assertEqual((homogeneous, kind), (True, 4))
        self.assertIs(diagnostics[0], tracker.occurrences[0])
        self.assertEqual(check_all_same_type(make_config(), ErrorTracker(), ['(R,S)']),
                         (True, 4, messages(diagnostics)))
        unrecognized = check_all_same_type(make_config(), ErrorTracker(), ['(bogus)'])
        self.assertIs(type(unrecognized[2]), str)

    def test_resolver_adapter_keeps_actual_worker_outcome_details(self):
        cfg = resolver_config()
        def run(function, tracker):
            details = {}
            worker = StubWorker(lambda backend, name: 'CCO' if name == 'ethanol' else None)
            with patch.object(name_resolver, '_ResolverWorker', return_value=worker):
                result = function(cfg, tracker, 'Synthesis of ethanol (1)', {},
                                  log=lambda _: None, details=details)
            return result, details, worker.calls
        tracker = ErrorTracker()
        typed, details, calls = run(name_resolver._smiles_and_name_from_paragraph, tracker)
        public, public_details, public_calls = run(name_resolver.smiles_and_name_from_paragraph, ErrorTracker())
        self.assertEqual(public, (*typed[:4], messages(typed[4])))
        self.assertEqual(public_details, details)
        self.assertEqual(public_calls, calls)
        self.assertIs(typed[4][0], tracker.occurrences[0])

    def test_hrms_variants_are_distinct_handles_and_public_list_identity_survives(self):
        text = 'HRMS (ESI): calcd for C2H7O+ [M+H]+ 47.0491, found 47.0501.'
        tracker = ErrorTracker()
        source = FindingSource(3, 8, 'hrms')
        typed, formula, mass = _check_hrms(make_config(), FindingSink(tracker, source), text, [])
        findings = [item for item in typed if isinstance(item, Finding)]
        ppm = [item for item in findings if item.code is ErrorCode.HRMS_PPM_TOO_LARGE]
        self.assertEqual([item.variant for item in ppm], ['default', 'values'])
        self.assertTrue(all(item.source == source for item in findings))
        self.assertEqual(tracker.get_count('hrms_ppm_too_large'), 2)
        output = ['Caller prefix']
        result = check_hrms(make_config(), ErrorTracker(), text, output)
        self.assertIs(result[0], output)
        self.assertEqual(result, (['Caller prefix', *messages(typed)], formula, mass))
        self.assertTrue(all(type(line) is str for line in output))

    def test_elemental_internal_handles_preserve_public_categories_and_science(self):
        text = ('Anal. Calcd for C2H6O: C, 52.14; H, 13.13. '
                'Found: C, 51.73; H, 13.13.')
        tracker = ErrorTracker()
        typed, formula, mass = _check_elemental_analysis(make_config(detailed=True), tracker, text, [])
        self.assertTrue(any(isinstance(item, Finding) and item.code is ErrorCode.EA_TOLERANCE_EXCEEDED
                            for item in typed))
        output = ['Prefix']
        result = check_elemental_analysis(make_config(detailed=True), ErrorTracker(), text, output)
        self.assertIs(result[0], output)
        self.assertEqual(result, (['Prefix', *messages(typed)], formula, mass))

    def test_formula_refusals_keep_unread_notation_as_an_uncounted_note(self):
        for producer, adapter, text in (
            (_check_hrms, check_hrms, 'HRMS (ESI): calcd for {}+ [M+H]+ 61.0648, found 61.0648.'),
            (_check_elemental_analysis, check_elemental_analysis,
             'Anal. Calcd for {}: C, 59.96; H, 13.42. Found: C, 59.96; H, 13.42.'),
        ):
            with self.subTest(producer=producer.__name__):
                tracker = ErrorTracker()
                typed, formula, mass = producer(make_config(), tracker, text.format('C3H8O_opaque'), [])
                self.assertEqual((formula, mass, tracker.occurrences), (None, None, ()))
                self.assertEqual([type(item) for item in typed], [AnalysisNote])
                self.assertIsNone(typed[0].highlight_hint)
                self.assertTrue(typed[0].text.startswith('Formula not checked by CheckSI'))
                tracker = ErrorTracker()
                typed, _, _ = producer(make_config(), tracker, text.format('Ng2C21H29'), [])
                self.assertEqual([item.variant for item in typed], ['unknown_element'])
                self.assertIs(typed[0], tracker.occurrences[0])
                self.assertEqual(adapter(make_config(), ErrorTracker(), text.format('Ng2C21H29'), [])[0],
                                 messages(typed))
