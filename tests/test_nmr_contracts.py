"""Named private NMR results retain legacy projections and diagnostic identity."""

import unittest
from unittest.mock import patch

import checksi.controller as controller

from checksi.analysis import nmr
from checksi.errors import ErrorCode
from checksi.errors.findings import Finding, messages
from checksi.errors.tracker import ErrorTracker
from tests.test_carbon_observations import config


def context(cfg, tracker, nucleus, **inputs):
    values = dict(trees_count=None, frequency_h=0,
                  atom_with_four_unique_substituents=0, atom_with_two_identical_substituents=0,
                  formula_nmr=None, e_formula=0, onh_count=0,
                  c13_count_alternatives=None, carbon_groups=None,
                  c13_count_alternatives_w_groups=None, stereogenic_signal_flag=False)
    values.update(inputs)
    return nmr.CheckContext(cfg, tracker, nucleus, **values)


class NMRResultContractTests(unittest.TestCase):
    def test_number_count_names_each_display_slot_without_reordering_occurrences(self):
        tracker = ErrorTracker()
        result = nmr._calculate_nmr_number_count(
            '20.0, 60.0, 20.0, 5, 7.12', 1, 0,
            context(config(), tracker, nmr.CARBON))
        self.assertIsInstance(result, nmr.NumberCountResult)
        self.assertEqual(result.count, 4)
        self.assertEqual([result.order, result.precision, result.rounding, result.duplicate],
                         list(tracker.occurrences))
        public = nmr.calculate_nmr_number_count(
            config(), ErrorTracker(), '20.0, 60.0, 20.0, 5, 7.12', '13C', 1, 1, None, 0, 0)
        self.assertIs(type(public), tuple)
        self.assertEqual(len(public), 7)
        self.assertEqual(public[0], result.count)
        self.assertEqual(public[-1], result.ratio)
        self.assertEqual(public[1:5], tuple(messages([item])[0] for item in
                         (result.order, result.rounding, result.duplicate, result.precision)))
        self.assertIs(type(public[5]), list)

    def test_named_signal_and_outer_results_share_preparation_and_public_eight_slots(self):
        groups, details, observations = [], {'stale': True}, ['stale']
        tracker = ErrorTracker()
        text = '13C NMR (100 MHz, CDCl3): δ 60.0.'
        preparation = nmr.DP5Preparation(refusal_details=details, observations=observations)
        result = nmr._check_nmr(text, context(config(), tracker, nmr.CARBON,
            trees_count=1, frequency_h=400, formula_nmr='CH4', carbon_groups=groups,
            preparation=preparation))
        self.assertIsInstance(result, nmr.NMRCheckResult)
        self.assertIsInstance(result.preparation, nmr.DP5Preparation)
        self.assertIs(result.preparation, preparation)
        self.assertIs(result.preparation.groups, groups)
        self.assertIs(result.preparation.refusal_details, details)
        self.assertIs(result.preparation.observations, observations)
        self.assertTrue(result.preparation.eligible)
        self.assertEqual(result.preparation.shifts, [60.0])
        public = nmr.check_nmr(config(), ErrorTracker(), text, '13C', 1, 1, 400,
                               0, 0, 'CH4', 0, 0, carbon_groups=groups)
        self.assertEqual(public, (messages(result.diagnostics), result.frequency, result.ratio,
                         result.section, result.eq_c_ratio, groups, True, [60.0]))
        self.assertIs(type(public), tuple)
        self.assertIs(type(public[0]), list)
        self.assertTrue(all(type(item) is str for item in public[0]))
        signal_context = context(config(), ErrorTracker(), nmr.CARBON, trees_count=1,
                                 formula_nmr='CH4', carbon_groups=groups)
        signal = nmr._analyze_nmr_signals('60.0', 100, signal_context)
        self.assertIsInstance(signal, nmr.SignalAnalysisResult)
        self.assertTrue(signal.preparation.eligible)
        self.assertIs(signal.preparation, signal_context.preparation)
        self.assertIs(signal.preparation.groups, groups)

    def test_typed_outer_display_keeps_the_original_finding_objects(self):
        tracker = ErrorTracker()
        result = nmr._check_nmr(
            '13C NMR (100 MHz, CDCl3): δ 20.0, 60.0, 20.0, 5, 7.12.',
            context(config(), tracker, nmr.CARBON, frequency_h=400))
        findings = [item for item in result.diagnostics if isinstance(item, Finding)]
        self.assertEqual(findings, [tracker.occurrences[i] for i in (0, 2, 3, 1)])
        self.assertTrue(all(any(item is occurrence for occurrence in tracker.occurrences)
                            for item in findings))


class NucleusContextTests(unittest.TestCase):
    def test_carbon_scope_uses_final_token_and_separator_preparation_status(self):
        import json
        for signals, reason in [('10.0 20.0', 'malformed_separators'),
                                ('10.0, 20.0, 30', 'incomplete_numeric_tokens'),
                                ('10.0, 20.0', None)]:
            with self.subTest(signals=signals):
                result = controller.CheckSIController(config())._analyze_single_description(
                    f'13C NMR (100 MHz, CDCl3): δ {signals}.', 'CCO', ErrorTracker())
                scope = result['carbon_scope']
                self.assertEqual(result['run_dp5'], reason is None)
                self.assertEqual(scope['applicability_status'], 'eligible' if reason is None else 'unassessed')
                self.assertEqual(scope['preparation_eligible'], reason is None)
                self.assertEqual(scope['preparation_reason'], reason)
                self.assertEqual(scope['full_environment_count'], 2)
                self.assertEqual(scope['observation_count'], len(result['carbon_observations']))
                self.assertEqual(scope['represented_signal_count'], 2)
                json.dumps(result, allow_nan=False)

    def test_unknown_structure_does_not_claim_a_complete_carbon_scope(self):
        scope = {}
        nmr.check_nmr(config(), ErrorTracker(), '13C NMR (100 MHz, CDCl3): δ 60.0, 20.0.',
                      '13C', 1, None, 400, 0, 0, 'C2H6O', 0, 0, carbon_scope_out=scope)
        self.assertIsNone(scope['full_environment_count'])
        self.assertFalse(scope['partition_complete'])
        self.assertEqual(scope['partition_reason'], 'structure_unavailable')

    def test_shared_carbon_preparation_has_json_native_full_scope_and_original_observations(self):
        import json
        from checksi.dp5_enrichment import _compound_request
        cfg = config()
        text = '13C NMR (100 MHz, CDCl3): δ 175.0, 60.0, 32.0, 20.0 (2C).'
        result = controller.CheckSIController(cfg)._analyze_single_description(
            text, 'CC(C)[C@H](N)C(=O)O', ErrorTracker())
        self.assertTrue(result['run_dp5'])
        self.assertEqual(result['dp5_c_signals_list'], [175., 60., 32., 20.])
        self.assertEqual(result['dp5_coincident_environments'], [[0, 2]])
        scope = result['carbon_scope']
        self.assertEqual((scope['full_environment_count'], scope['observation_count'],
                          scope['represented_signal_count']), (5, 4, 5))
        self.assertEqual(scope['accepted_missing_direct_cb_count'], 0)
        self.assertEqual(scope['applicability_status'], 'eligible')
        json.dumps(result, allow_nan=False)
        request = _compound_request(cfg, {**result, 'smiles': 'CC(C)[C@H](N)C(=O)O'}, 1)
        self.assertEqual(request.carbon_shifts, (175., 60., 32., 20.))
        self.assertEqual(request.coincident_environments, ((0, 2),))

    def test_controller_maps_real_structure_counts_flags_and_distinct_hydrogen_carbon_results(self):
        expected = [('1H', 0), ('19F', 2), ('11B', 3), ('31P', 4),
                    ('29Si', 5), ('2H', 6), ('15N', 7), ('13C', 1)]
        self.assertEqual([(item.label, item.atom) for item in nmr.NUCLEUS_SPECS], expected)
        text = ' '.join(f'{label} NMR ({frequency} MHz, CDCl3): δ {signals}.'
            for label, frequency, signals in [('1H', 123, '3.00 (s, 18H)'),
                ('19F', 376.4, '10.00'), ('11B', 128.6, '10.0'), ('31P', 161.9, '10.0'),
                ('29Si', 79.4, '10.0'), ('2H', 61.4, '10.0'), ('15N', 40.6, '10.0'),
                ('13C', 100.6, '90.0')])
        calls = []
        original = controller._check_nmr
        def observe(description, context):
            result = original(description, context)
            calls.append(context)
            return result
        with patch.object(controller, '_check_nmr', side_effect=observe):
            result = controller.CheckSIController(config())._analyze_single_description(text, 'CC(F)(C)C', ErrorTracker())
        self.assertEqual([(item.nucleus.label, item.nucleus.atom) for item in calls], expected)
        self.assertEqual([item.frequency_h for item in calls], [0] + [123.0] * 7)
        self.assertEqual([(item.trees_count, item.e_formula) for item in calls],
                         [(0, 0), (1, 1), (0, 0), (0, 0), (0, 0), (None, 0), (None, 0), (2, 1)])
        self.assertTrue(all(item.formula_nmr == 'C4H9F' and item.onh_count == 0 for item in calls))
        self.assertTrue(all(item.carbon_groups is None and item.c13_count_alternatives is None
                            for item in calls[:-1]))
        self.assertEqual(calls[-1].c13_count_alternatives, {2})
        self.assertEqual(result['ratio'], 1.0)
        self.assertEqual(result['eq_c_ratio'], .25)
        self.assertIs(type(result['ratio']), float)
        self.assertIs(type(result['eq_c_ratio']), float)
        self.assertEqual(result['hnmr_section_text'], ' (123 MHz, CDCl3): δ 3.00 (s, 18H)')
        self.assertEqual(result['cnmr_section_text'], ' (100.6 MHz, CDCl3): δ 90.0')
        self.assertEqual(result['dp5_c_signals_list'], [90.0])
        self.assertFalse(result['run_dp5'])
        self.assertEqual(result['dp5_refusal_details'], {
            'observed_count': 1, 'prepared_shift_count': 1, 'required_count': 2, 'cause': 'count_mismatch'})
        self.assertTrue(any('nmr_frequency_mismatch.' in line for line in result['warnings']))

    def test_controller_carries_real_stereo_alternatives_and_exchangeable_protons_to_carbon(self):
        text = ('1H NMR (400 MHz, CDCl3): δ 3.00 (s, 18H). '
                '13C NMR (100.6 MHz, CDCl3): δ 100.0 (2C), 90.0, 80.0, 70.0.')
        calls = []
        original = controller._check_nmr
        def observe(description, context):
            calls.append(context)
            return original(description, context)
        with patch.object(controller, '_check_nmr', side_effect=observe):
            result = controller.CheckSIController(config())._analyze_single_description(text, 'CC(O)C(C)C', ErrorTracker())
        carbon = calls[-1]
        self.assertEqual(carbon.trees_count, 5)
        self.assertEqual(carbon.c13_count_alternatives, {4, 5})
        self.assertIsNotNone(carbon.c13_count_alternatives_w_groups)
        self.assertEqual([(item.name, item.count) for item in carbon.c13_count_alternatives_w_groups],
                         [("formal", 5), ("diastereotopic_merged", 4)])
        self.assertTrue(carbon.stereogenic_signal_flag)
        self.assertTrue(all(item.formula_nmr == 'C5H12O' and item.onh_count == 1 for item in calls))
        self.assertEqual(result['ratio'], 2.0)
        self.assertEqual(result['eq_c_ratio'], 1.0)
        self.assertEqual(result['dp5_carbon_groups'], [])  # All five formal environments are singletons.
        self.assertEqual(result['dp5_c_signals_list'], [100.0, 90.0, 80.0, 70.0])
        self.assertEqual([item['represented_atoms'] for item in result['carbon_observations']],
                         [2, None, None, None])
        self.assertTrue(result['run_dp5'])
        self.assertEqual(result['dp5_refusal_details'], {})
        self.assertFalse(any('c13_count_structure_check_stereo.' in line for line in result['warnings']))

    def test_scientific_context_inputs_must_be_explicit(self):
        values = dict(trees_count=1, frequency_h=400, atom_with_four_unique_substituents=0,
                      atom_with_two_identical_substituents=0, formula_nmr='CHF3', e_formula=1,
                      onh_count=0, c13_count_alternatives=None, carbon_groups=None,
                      c13_count_alternatives_w_groups=None, stereogenic_signal_flag=False)
        for omitted in values:
            with self.subTest(omitted=omitted), self.assertRaises(TypeError):
                nmr.CheckContext(config(), ErrorTracker(), nmr.CARBON,
                                 **{name: value for name, value in values.items() if name != omitted})

    def test_legacy_label_and_selector_stay_independent(self):
        public = nmr.check_nmr(config(), ErrorTracker(), '19F NMR (100 MHz, CDCl3): δ 60.0.',
                               '19F', 1, 1, 0, 0, 0, 'CH4', 0, 0, carbon_groups=[])
        self.assertTrue(public[6])
        self.assertEqual(public[7], [60.0])


class ParsedSectionTests(unittest.TestCase):
    def test_parse_facts_keep_missing_empty_delta_and_raw_normalized_sections_distinct(self):
        for text, raw, signals in (
            ('No spectrum.', None, None),
            ('13C NMR.', '', None),
            ('13C NMR (100 MHz, CDCl3): 60.0.', ' (100 MHz, CDCl3): 60.0', None),
            ('13C NMR (100 MHz, CDCl3): δ .', ' (100 MHz, CDCl3): δ ', ''),
        ):
            with self.subTest(text=text):
                parsed = nmr._parse_nmr_section(text, '13C')
                self.assertIsInstance(parsed, nmr.ParsedSection)
                self.assertEqual(parsed.raw_text, raw)
                self.assertEqual(parsed.signals, signals)
        parsed = nmr._parse_nmr_section(
            '13C{19F, 1H} NMR (100 MHz, CDCl3): δ 130.0–131.0 (2C).', '13C')
        self.assertIn('130.0–131.0', parsed.raw_text)
        self.assertIn('130.0-131.0', parsed.normalized_text)
        self.assertEqual(parsed.signals, '130.0-131.0 (2C)')
        self.assertEqual(parsed.fluorine_decoupled, 1)

    def test_legacy_buffers_clear_before_selection_even_when_selection_raises(self):
        for atom in range(8):
            details, observations = {'stale': True}, ['stale']
            def refuse(text, nuclei):
                self.assertEqual(observations, [])
                self.assertEqual(details, {} if atom == 1 else {'stale': True})
                raise RuntimeError('selection stopped')
            with self.subTest(atom=atom), patch.object(nmr, '_extract_nmr_section', side_effect=refuse):
                with self.assertRaisesRegex(RuntimeError, 'selection stopped'):
                    nmr.check_nmr(config(), ErrorTracker(), 'No spectrum', '13C', atom,
                        None, 0, 0, 0, None, 0, 0, dp5_refusal_details=details, observations_out=observations)

    def test_carbon_observations_reach_legacy_buffer_before_counting_exception(self):
        details, observations = {'stale': True}, ['stale']
        def refuse(*args):
            self.assertEqual([item.shift for item in observations], [60.0, 20.0])
            self.assertEqual(details, {})
            raise RuntimeError('counting stopped')
        with patch.object(nmr, '_calculate_nmr_number_count', side_effect=refuse):
            with self.assertRaisesRegex(RuntimeError, 'counting stopped'):
                nmr.check_nmr(config(), ErrorTracker(), '13C NMR (100 MHz, CDCl3): δ 60.0, 20.0.',
                    '13C', 1, 2, 400, 0, 0, 'C2H6', 0, 0,
                    dp5_refusal_details=details, observations_out=observations)
        self.assertEqual([item.raw_token for item in observations], ['60.0', '20.0'])


class MovedNMRBehaviorTests(unittest.TestCase):
    def test_inventory_refusal_retains_gate_without_duplicate_structure_finding(self):
        for trees in (2, 3):
            for disabled in (False, True):
                with self.subTest(trees=trees, disabled=disabled):
                    tracker, details = ErrorTracker(), {}
                    checked = nmr.check_nmr(config(disabled), tracker,
                        '13C NMR (100 MHz, CDCl3): δ 60.0 (3C), 20.0.',
                        '13C', 1, trees, 400, 0, 0, 'C4H10O', 0, 0,
                        dp5_refusal_details=details)
                    self.assertFalse(checked[6])
                    self.assertEqual(details['cause'], 'annotation_inventory_mismatch')
                    lines = [line for line in checked[0] if 'Carbon observations represent' in line]
                    self.assertEqual(lines, [])
                    self.assertEqual(tracker.get_count('c13_count_structure_check'), int(not disabled))

    def test_public_checks_keep_codes_order_nitrogen_context_and_fluorine_decoupling(self):
        known_codes = {code.value for code in ErrorCode}
        cases = (
            ('1H', 0, None, 'CH4', 0, '1.20 (s, 1H) HRMS ESI', 400,
             ['bad_ending_nmr', 'h_count_mismatch']),
            ('1H', 0, None, 'CH4', 0, '1.20 (s, 0.5H)', 400,
             ['h_count_not_integer', 'h_count_mismatch']),
            ('1H', 0, None, 'CH4', 0, '1.20 (s)', 400,
             ['hnmr_h_count_missing', 'hnmr_signal_desc_invalid', 'hnmr_missing_or_uncounted', 'h_count_mismatch']),
            ('19F', 2, 1, 'CH4', 0, '10.00 (d)', 376.4,
             ['nmr_j_missing', 'nmr_mentioned_but_element_missing']),
            ('15N', 7, 2, 'CH4', 0, '10.0', 40.6,
             ['nmr_mentioned_but_element_missing']),
            ('13C{19F}', 1, 1, 'CHF3', 1, '60.0', 100.6, []),
            ('13C', 1, 1, 'CHF3', 1, '60.0', 100.6, ['c13_coupling_missing_with_f']),
        )
        for heading, atom, trees, formula, flag, signals, frequency, expected in cases:
            with self.subTest(heading=heading, signals=signals):
                tracker = ErrorTracker()
                label = heading.split('{')[0]
                checked = nmr.check_nmr(config(), tracker,
                    f'{heading} NMR ({frequency} MHz, CDCl3): δ {signals}.',
                    label, atom, trees, 400, 0, 0, formula, flag, 0)
                displayed_codes = [line.removeprefix('❗ ').removeprefix('⚠️ ').split('. ', 1)[0]
                                   for line in checked[0]]
                self.assertEqual([code for code in displayed_codes if code in known_codes], expected)
                self.assertEqual(checked[1], frequency)
                if atom == 7:
                    self.assertEqual(tracker.occurrences[0].context['element_name'], 'N')
                    self.assertIn('but N is not present', checked[0][0])
                if atom == 1:
                    self.assertTrue(checked[6])
                    self.assertEqual(checked[7], [60.0])


class FrequencyCompatibilityTests(unittest.TestCase):
    def test_invalid_frequency_keeps_the_selected_display_label(self):
        result = nmr.check_nmr(config(), ErrorTracker(), '19F NMR (440 MHz, CDCl3): δ 60.0.',
                               '19F', 1, 1, 0, 0, 0, 'CH4', 0, 0, carbon_groups=[])
        self.assertEqual([line for line in result[0] if 'nmr_frequency_invalid.' in line], [
            '❗ nmr_frequency_invalid. The 19F NMR frequency (440 MHz) is not among the common frequencies; check it manually.'])

    def test_missing_frequency_does_not_require_a_recognized_selector(self):
        warning, frequency, mismatch = nmr.extract_and_compare('CDCl3', 99, 0)
        self.assertTrue(warning)
        self.assertIs(type(frequency), int)
        self.assertEqual(frequency, 0)
        self.assertIsNone(mismatch)

    def test_public_frequency_preserves_numeric_types_and_binary_tolerance_endpoint(self):
        for header, expected_type in (('CDCl3', int), ('0 MHz, CDCl3', float)):
            with self.subTest(header=header):
                result = nmr.check_nmr(config(), ErrorTracker(), f'1H NMR ({header}): δ 1.20 (s, 1H).',
                                       '1H', 0, None, 0, 0, 0, None, 0, 0)
                self.assertIs(type(result[1]), expected_type)
        result = nmr.check_nmr(config(), ErrorTracker(), '2H NMR (62.15 MHz, CDCl3): δ 10.0.',
                               '2H', 6, None, 398.37133550488596, 0, 0, None, 0, 0)
        self.assertEqual(result[1], 62.15)
        self.assertEqual(result[0], [
            '❗ nmr_frequency_mismatch. The frequency of 2H NMR does not match the frequency of 1H NMR. Should be 61.1. Check manually.'])

    def test_precision_threshold_remains_strictly_above_299_and_always_two_for_protons(self):
        for frequency in (299, 299.0001):
            for nucleus, signal, expected in ((nmr.CARBON, '60.0', frequency > 299),
                                               (nmr.PROTON, '1.2 (s, 1H)', True)):
                tracker = ErrorTracker()
                nmr._analyze_nmr_signals(signal, frequency, context(config(), tracker, nucleus))
                self.assertEqual(tracker.get_count('nmr_signal_precision_invalid'), int(expected))


if __name__ == '__main__':
    unittest.main()
