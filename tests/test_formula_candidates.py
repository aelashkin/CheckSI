"""Require complete analytical formula candidates before any calculation."""

import time
import unittest

from checksi.analysis.elemental import check_elemental_analysis
from checksi.analysis.hrms import check_hrms
from checksi.analysis.hrms_fields import find_ea_formula_candidate, find_hrms_formula, find_hrms_formula_candidate
from checksi.controller import CheckSIController
from checksi.utils.error_handler import ErrorTracker
from tests.test_hrms import make_config


HRMS_REFUSALS = (
    "[13C] C5H13O2", "[13C]6 H6", "C [13C]H5O",
    "2C2H6O", "2 C2H6O", "C2H6 [18O]", "C2 H6O", "C2H6O·H2O", "C2H6O.H2O",
    "C2H6O . H2O", "C2H6O(H2O)", "C2H6O_opaque", "C2H6O C3H8O",
    "xC2H6O", "C2H6O[H2O]", "C2H6O + H2O", "C2H6O\nH2O",
    "C2H6O.O", "C2H6O.Na", "C2H6O.[18O]", "C2H6O.unknown",
    "C2H6O 0.5 H2O", "C2H6O 0.5000 H2O",
    "C2H6O 0.5H2O", "C2H6O 0.5000H2O",
)
EA_REFUSALS = (*(token for token in HRMS_REFUSALS if token not in {"C2H6O·H2O", "C2H6O.H2O"}), "C2H6[18O](H)")


def hrms_text(token):
    return f"HRMS (ESI): calcd for {token}+ [M+H]+ 47.0491, found 47.0491."


def ea_text(token):
    return (f"Anal. Calcd for {token}: C, 52.14; H, 13.13; O, 34.73. "
            "Found: C, 52.14; H, 13.13; O, 34.73.")


class CompleteFormulaTests(unittest.TestCase):
    def test_no_calculated_cue_retains_each_leading_isotope_before_formula(self):
        for isotope in ('[18O]', '[2H]', '[34S]', '[13C]'):
            for space in ('', ' ', '\n'):
                with self.subTest(isotope=isotope, space=space):
                    token = isotope + space + 'C5H13O2'
                    text = f'HRMS (ESI): {token}+ [M+H]+ 100.0000, found 100.0000.'
                    details = {}
                    _, formula, mass = check_hrms(make_config(overrides={'formula_invalid_format': 'disabled'}), ErrorTracker(), text, [], details=details)
                    self.assertEqual(details['candidate']['raw'], token)
                    self.assertEqual(details['usable'], not space)
                    if space:
                        self.assertIsNone(formula)
                        self.assertFalse(mass)
                    else:
                        self.assertEqual(formula, token)
                        self.assertGreater(mass, 0)
        text = 'HRMS (ESI): C5H13O2+ [M+H]+ 100.0000, found 100.0000.'
        self.assertEqual(find_hrms_formula(text).formula, 'C5H13O2')

    def test_candidate_offsets_and_reused_details_retain_raw_complete_evidence(self):
        for find, check, text in ((find_hrms_formula_candidate, check_hrms, hrms_text("C2 H6O")),
                                  (find_ea_formula_candidate, check_elemental_analysis, ea_text("C2H6[18O](H)"))):
            candidate = find(text)
            self.assertEqual(text[candidate.start:candidate.end], candidate.raw)
            details = {'stale': True}
            check(make_config(overrides={'formula_invalid_format': 'disabled'}), ErrorTracker(), text, [], details=details)
            self.assertNotIn('stale', details)
            self.assertEqual(details['candidate'], candidate.to_dict())
            self.assertEqual(details['status'], 'unsupported')
            self.assertFalse(details['usable'])
            check(make_config(), ErrorTracker(), 'No analytical formula.', [], details=details)
            self.assertEqual(details['status'], 'absent')
            self.assertIsNone(details['candidate'])

    def test_terminal_period_and_analysis_label_end_candidates(self):
        for ending in ('. Found', ' Found', ' 13C NMR', '; Found', ': Found', ', found'):
            candidate = find_hrms_formula_candidate('HRMS calcd for C2H6O' + ending)
            self.assertEqual(candidate.formula, 'C2H6O')

    def test_ion_label_after_calculated_cue_does_not_become_formula_prefix(self):
        for ion in ('[M+H]+', '[2M+H]+', '[C2H7O]+', '([M+H]+)', '(ESI, [M+H]+)'):
            text = f'HRMS calcd for {ion} C2H7O+ 47.0491, found 47.0491.'
            match = find_hrms_formula(text)
            self.assertIsNotNone(match)
            self.assertEqual(match.formula, 'C2H7O')
            self.assertEqual(text[match.formula_start:match.formula_end], 'C2H7O')

    def test_wrapped_ion_after_formula_preserves_formula_and_inverse_consumers(self):
        for ion in ('([M+H]+)', '( [M+H]+)', '(ESI, [M+H]+)'):
            text = f'HRMS (ESI) calcd for C7H8FO+ {ion} 127.0554, found 127.0554.'
            details = {}
            _, formula, _ = check_hrms(make_config(), ErrorTracker(), text, [], details=details)
            self.assertEqual(formula, 'C7H8FO')
            self.assertTrue(details['usable'])

    def test_large_digit_runs_keep_full_refusals_without_prefix_rescue(self):
        raw = 'C' + '9' * 16000 + '('
        self.assertIsNone(find_hrms_formula_candidate('HRMS calcd for ' + raw).formula)
        self.assertIsNone(find_hrms_formula_candidate('HRMS ' + '9' * 16000 + ' opaque'))
        candidate = find_hrms_formula_candidate('HRMS 2 C2H6O+ 47.0491, found 47.0491.')
        self.assertEqual(candidate.raw, '2 C2H6O')
        self.assertIsNone(candidate.formula)

    def test_known_bracketed_instrument_labels_do_not_become_formula_prefixes(self):
        for label in ('[ESI]', '[ESI-TOF]'):
            self.assertEqual(find_hrms_formula(f'HRMS {label} calcd for C2H7O+ 47.0491, found 47.0491.').formula, 'C2H7O')

    def test_numeric_citations_do_not_become_formula_tokens(self):
        for citation in ('[12]', '[3, 4]', '[5–7]'):
            text = f'HRMS spectra were recorded {citation}. C2H6O was used as a solvent.'
            candidate = find_hrms_formula_candidate(text)
            self.assertEqual((candidate.raw, candidate.formula), ('C2H6O', 'C2H6O'))

    def test_non_formula_brackets_do_not_seed_candidates(self):
        nmr = '1H NMR (400 MHz, CDCl3): δ 1.00 (s, 6H). '
        for block in ('HRMS (ESI) m/z 61.0648 [M+H]+. [α]D20 +12.0 (c 1.0, CHCl3).',
                      'HRMS (ESI) data matched the literature [S3].',
                      'HRMS (ESI) data matched the literature [12a].',
                      'HRMS (ESI) data matched the literature [ref. 12].'):
            with self.subTest(block=block):
                self.assertIsNone(find_hrms_formula_candidate(block))
                result = CheckSIController(make_config(), event_dispatcher=lambda _: None).run_analysis([nmr + block])
                self.assertNotIn('formula_notation_not_supported',
                                 [record['reason'] for record in result['assessment']['unassessed']])
        # A bracket that is a formula or isotope label, or directly precedes one, still seeds.
        for text, raw in (('HRMS calcd for [13C]C5H13O2+ 100.0000', '[13C]C5H13O2'),
                          ('HRMS [opaque] C2H6O+ 47.0491', '[opaque] C2H6O'),
                          ('HRMS [CHCl3] 47.0491', '[CHCl3]')):
            with self.subTest(text=text):
                self.assertEqual(find_hrms_formula_candidate(text).raw, raw)

    def test_recognized_instrument_closing_punctuation_can_touch_the_formula(self):
        for header in ('HRMS (ESI)', 'HRMS[ESI]', 'HRMS [ESI]', 'HRMS (ESI-TOF)'):
            for separator in ('', ':', ' ', ': '):
                text = f'{header}{separator}C2H7O+ 47.0491, found 47.0491.'
                candidate = find_hrms_formula_candidate(text)
                self.assertEqual(candidate.formula, 'C2H7O')
                self.assertEqual(text[candidate.start:candidate.end], 'C2H7O')
        for prefix in ('[H2O]', '[18O2]', '[opaque]', '([H2O])'):
            self.assertIsNone(find_hrms_formula_candidate(f'HRMS {prefix}C2H7O+ 47.0491, found 47.0491.').formula)

    def test_no_cue_numeric_prefix_keeps_its_complete_sign_and_fraction(self):
        for prefix in ('0.5', '-2', '+2', '1/2', '.5'):
            for header in ('HRMS ', 'HRMS (ESI)'):
                raw = prefix + ' C2H6O'
                text = f'{header}{raw}+ [M+H]+ 47.0491, found 47.0491.'
                candidate = find_hrms_formula_candidate(text)
                self.assertIsNone(candidate.formula)
                self.assertEqual(candidate.raw, raw)
                self.assertEqual(text[candidate.start:candidate.end], raw)

    def test_whitespace_runs_retain_the_full_original_candidate_span(self):
        raw = 'C2H6O' + ' ' * 8000 + 'H2O'
        text = f'HRMS calcd for {raw} 100.0000, found 100.0000.'
        candidate = find_hrms_formula_candidate(text)
        self.assertEqual(candidate.raw, raw)
        self.assertEqual(text[candidate.start:candidate.end], raw)
        self.assertIsNone(candidate.formula)

    def test_unknown_bracket_prefix_is_retained_with_or_without_calculated_cue(self):
        for prefix in ('[H2O]', '[18O2]', '[opaque]'):
            for cue in ('', 'calcd for '):
                text = f'HRMS {cue}{prefix} C2H6O+ [M+H]+ 47.0491, found 47.0491.'
                candidate = find_hrms_formula_candidate(text)
                self.assertEqual(candidate.raw, prefix + ' C2H6O')
                self.assertIsNone(candidate.formula)

    def test_measurement_precision_does_not_change_supported_formula_status(self):
        for mass in ('47', '47.', '47.049', '47.04910', '47.04912345', '0.0000', '-1.0000'):
            text = f'HRMS calcd for C2H7O+ {mass}, found 47.0491.'
            details = {}
            tracker = ErrorTracker()
            _, formula, _ = check_hrms(make_config(), tracker, text, [], details=details)
            self.assertEqual(formula, 'C2H7O')
            self.assertTrue(details['usable'])
            self.assertEqual(tracker.get_count('formula_invalid_format'), 0)
            self.assertEqual(tracker.get_count('hrms_calcd_mass_missing'), 1)
        for tail in ('.', '. Found 47.0491.', ' 47.0491.'):
            text = 'HRMS calcd for C2H7O+' + tail
            candidate = find_hrms_formula_candidate(text)
            self.assertEqual(candidate.formula, 'C2H7O')

    def test_known_ea_annotations_and_value_fields_end_the_formula(self):
        for delimiter in (' (M.W. 46.07): ', ' (cf. ref. 1): ', ' ', '. '):
            text = 'Anal. Calcd for C2H6O' + delimiter + 'C, 52.14; H, 13.13; O, 34.73. Found: C, 52.14; H, 13.13; O, 34.73.'
            _, formula, mass = check_elemental_analysis(make_config(), ErrorTracker(), text, [])
            self.assertEqual(formula, 'C2H6O')
            self.assertTrue(mass)

    def test_composition_state_words_keep_the_complete_token(self):
        for words in ('hydrate', 'hemihydrate', 'x H2O', 'hydrochloride', 'sodium salt',
                      'trifluoroacetate', 'with 0.5 H2O', 'and H2O', 'methanol solvate'):
            with self.subTest(words=words):
                raw = f'C3H8O {words}'
                ea = find_ea_formula_candidate(f'Anal. Calcd for {raw}: C, 46.13; H, 12.90.')
                hrms = find_hrms_formula_candidate(f'HRMS calcd for {raw} 61.0648, found 61.0648.')
                for candidate in (ea, hrms):
                    self.assertEqual(candidate.raw, raw)
                    self.assertIsNone(candidate.formula)
        for connector in ('requires', 'is', 'was', 'found'):
            with self.subTest(connector=connector):
                self.assertEqual(find_hrms_formula_candidate(f'HRMS C3H8O {connector} 60.0570').formula, 'C3H8O')
                self.assertEqual(find_ea_formula_candidate(f'Anal. Calcd for C3H8O {connector} C, 59.96').formula, 'C3H8O')

    def test_decimal_counts_stay_inside_the_complete_token(self):
        for raw in ('C17H19.4NO3.2', 'C10H13O2.5'):
            with self.subTest(raw=raw):
                candidate = find_ea_formula_candidate(f'Anal. Calcd for {raw}: C, 70.10; H, 6.71.')
                self.assertEqual((candidate.raw, candidate.formula), (raw, raw))
        # A sentence end before a glued NMR label still ends the formula.
        self.assertEqual(find_hrms_formula_candidate('HRMS calcd for C2H6O2.13C NMR (100 MHz)').formula, 'C2H6O2')

    def test_unclosed_ea_metadata_runs_keep_a_linear_scan(self):
        for unit in ('(MW ', '(M.W. ', '(cf. ref. '):
            with self.subTest(unit=unit):
                text = 'Anal. Calcd for C2H6O ' + unit * 8000 + ': C, 52.14. Found: C, 52.14.'
                started = time.perf_counter()
                candidate = find_ea_formula_candidate(text)
                self.assertLess(time.perf_counter() - started, 1.0)
                self.assertIsNone(candidate.formula)

    def test_hrms_refuses_full_unsupported_candidate_without_suffix_mass(self):
        for token in HRMS_REFUSALS:
            with self.subTest(token=token):
                tracker = ErrorTracker()
                lines, formula, mass = check_hrms(make_config(), tracker, hrms_text(token), [])
                self.assertIsNone(formula)
                self.assertFalse(mass)
                # Unsupported notation is reported as not checked, not as an author error.
                note, = lines
                self.assertIn(token, note)
                self.assertIn("not checked by CheckSI", note)
                self.assertEqual(tracker.get_count("formula_invalid_format"), 0)
                self.assertEqual(tracker.get_count("calcd_formula_mass_mismatch"), 0)
                self.assertIsNone(find_hrms_formula(hrms_text(token)))

    def test_ea_refuses_full_unsupported_candidate_without_partial_composition(self):
        for token in EA_REFUSALS:
            with self.subTest(token=token):
                tracker = ErrorTracker()
                lines, formula, mass = check_elemental_analysis(make_config(), tracker, ea_text(token), [])
                self.assertIsNone(formula)
                self.assertFalse(mass)
                note, = lines
                self.assertIn(token, note)
                self.assertIn("not checked by CheckSI", note)
                self.assertEqual(tracker.get_count("formula_invalid_format"), 0)
                self.assertEqual(tracker.get_count("ea_formula_calcd_mismatch"), 0)
                self.assertEqual(tracker.get_count("formula_not_found"), 0)

    def test_muting_refusals_cannot_make_partial_formula_usable(self):
        for check, text, author_error in ((check_hrms, hrms_text("[13C] C5H13O2"), False),
                                          (check_elemental_analysis, ea_text("C2H6[18O](H)"), False),
                                          (check_hrms, hrms_text("C2.1H6O"), True),
                                          (check_elemental_analysis, ea_text("C2H6[18O"), True)):
            for severity in ("disabled", "info", "warning", "critical"):
                with self.subTest(check=check.__name__, text=text, severity=severity):
                    tracker = ErrorTracker()
                    details = {}
                    lines, formula, mass = check(make_config(overrides={"formula_invalid_format": severity}), tracker, text, [],
                                                 details=details)
                    self.assertIsNone(formula)
                    self.assertFalse(mass)
                    self.assertFalse(details["usable"])
                    self.assertEqual(details["status"], "invalid" if author_error else "unsupported")
                    finding = int(author_error and severity != "disabled")
                    self.assertEqual(tracker.get_count("formula_invalid_format"), finding)
                    self.assertEqual(len(lines), finding if author_error else 1)

    def test_internal_hrms_isotopes_and_adjacent_delimiters_remain_supported(self):
        for token in ("C5[13C]H13O2", "C2H5[2H]", "C2H5[37Cl]2", "C2H6[34S]", "C2H6[18O]"):
            for delimiter in ("+ [M+H]+ ", "+[2M+H]+ ", "+[C2H7O]+ ", "+: ", "+; ", "+, ", " "):
                with self.subTest(token=token, delimiter=delimiter):
                    text = f"HRMS [M+[13C]H3]+ calcd for {token}{delimiter}100.0000, found 100.0000."
                    match = find_hrms_formula(text)
                    self.assertIsNotNone(match)
                    self.assertEqual(match.formula, token)
                    self.assertEqual(text[match.formula_start:match.formula_end], token)

    def test_controller_records_not_checked_formula_as_a_coverage_gap(self):
        cfg = make_config(overrides={"formula_invalid_format": "disabled"})
        text = "1H NMR (400 MHz, CDCl3): δ 1.00 (s, 6H). " + hrms_text("[13C] C5H13O2")
        result = CheckSIController(cfg).run_analysis([text])
        self.assertEqual(result["assessment"]["status"], "partial")
        record, = result["assessment"]["unassessed"]
        self.assertEqual((record["reason"], record["excerpt"]), ("formula_notation_not_supported", "[13C] C5H13O2"))
        source = result["compounds"][0]["formula_sources"]["hrms"]
        self.assertEqual(source["candidate"]["raw"], "[13C] C5H13O2")
        self.assertEqual(source["status"], "unsupported")
        self.assertFalse(source["usable"])
        self.assertEqual(source["span_basis"], "normalized_description")
        self.assertEqual(result["compounds"][0]["original_text"], text)


if __name__ == "__main__":
    unittest.main()
