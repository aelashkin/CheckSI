"""Identify complete organic formula assertions without accepting partial tokens."""

import unittest

from molmass import Formula

from checksi.analysis.hrms import check_hrms
from checksi.analysis.hrms_fields import find_hrms_formula_candidate, iter_ion_expression_spans
from checksi.analysis.hrms_ions import molecular_radical_evidence
from checksi.controller import CheckSIController
from checksi.utils.error_handler import ErrorTracker
from tests.test_formula_reconciliation import NMR
from tests.test_hrms import make_config


ELECTRON_MASS = 0.000548579909065


def spectrum(header, formula, raw=None, charge=1, ion='[M+H]+'):
    mz = (Formula(formula).isotope.mass - charge * ELECTRON_MASS) / abs(charge)
    declaration = (raw if raw is not None else formula) + ('+' if charge == 1 else f'+{charge}')
    return f'{header} {ion} calcd for {declaration} {mz:.4f}, found {mz:.4f}. '


class CompleteOrganicFormulaIdentification(unittest.TestCase):
    def test_single_carbon_and_contiguous_leading_isotope_atoms_are_complete(self):
        for formula in ('CH5', 'C[13C]H5O', '[12C]H5', '[13C]C5H13O2', '[12C]6H7O', '[18O]C2H7O'):
            with self.subTest(formula=formula):
                text = spectrum('HRMS', formula)
                candidate = find_hrms_formula_candidate(text)
                self.assertIsNotNone(candidate)
                self.assertEqual(candidate.raw, formula)
                self.assertEqual(candidate.formula, formula)
                self.assertEqual(text[candidate.start:candidate.end], candidate.raw)
                tracker = ErrorTracker()
                _, checked, mass = check_hrms(make_config(), tracker, text, [])
                self.assertEqual(checked, formula)
                self.assertGreater(mass, 0)
                self.assertEqual(tracker.get_count('calcd_formula_mass_mismatch'), 0)
                self.assertEqual(tracker.get_count('formula_invalid_format'), 0)

    def test_common_ei_and_qtof_headers_use_the_shared_identification(self):
        for header in ('HREIMS', 'HR-EI-MS', 'QTOF MS', 'Q-TOF MS', 'HRMS (APCI)'):
            with self.subTest(header=header):
                text = spectrum(header, 'C2H7O')
                candidate = find_hrms_formula_candidate(text)
                self.assertIsNotNone(candidate)
                self.assertEqual(candidate.formula, 'C2H7O')

    def test_publication_cues_and_instrument_words_are_not_formula_atoms(self):
        for cue in ('Calcd for', 'Calculated mass for', 'CALCD FOR', 'CALCULATED MASS FOR'):
            with self.subTest(cue=cue):
                self.assertEqual(find_hrms_formula_candidate(spectrum('HRMS (APCI)', 'CH5').replace('calcd for', cue)).formula, 'CH5')

    def test_ei_header_variants_keep_the_neutral_parent_basis(self):
        for header in ('HREIMS', 'HR-EI-MS'):
            with self.subTest(header=header):
                tracker = ErrorTracker()
                text = spectrum(header, 'C2H6O', ion='[M]+')
                result = CheckSIController(make_config())._analyze_single_description(NMR + text, 'CCO', tracker)
                self.assertEqual(result['formula_sources']['hrms_neutral']['composition']['state'], 'neutral')
                self.assertEqual(result['formula_comparisons'][-1]['status'], 'agree')
                self.assertEqual(tracker.get_count('formula_source_review_required'), 0)

    def test_later_ei_instrument_cannot_qualify_an_earlier_esi_parent(self):
        text = spectrum('HRMS (ESI)', 'C2H6O', ion='[M]+')
        self.assertFalse(molecular_radical_evidence(text + spectrum('EI-HRMS', 'C2H6O', ion='[M]+')))

    def test_later_header_radical_does_not_qualify_an_earlier_formula_without_ion_label(self):
        first = 'HRMS (ESI) calcd for C2H6O+ 46.0413, found 46.0413. '
        for header in ('HREIMS', 'HR-EI-MS', 'EI-TOF', 'QTOF MS', 'ESI', '(ESI)', '(ESI-TOF)'):
            with self.subTest(header=header):
                self.assertFalse(molecular_radical_evidence(first + spectrum(header, 'C2H6O', ion='[M]•+')))

    def test_owned_postformula_instrument_wrapper_retains_radical_evidence(self):
        for mode in ('ESI', 'ESI-TOF', 'APCI'):
            with self.subTest(mode=mode):
                text = f'HRMS calcd for C2H6O+ ({mode}, [M]•+) 46.0413, found 46.0413.'
                self.assertTrue(molecular_radical_evidence(text))

    def test_bracketed_molecular_formula_preserves_raw_span_and_charge_magnitude(self):
        formula = 'C3H10O'
        mz = (Formula(formula).isotope.mass - 2 * ELECTRON_MASS) / 2
        text = f'HRMS calcd for [{formula}]2+ {mz:.4f}, found {mz:.4f}.'
        candidate = find_hrms_formula_candidate(text)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.raw, f'[{formula}]')
        self.assertEqual(candidate.formula, formula)
        self.assertEqual(candidate.charge_raw, '2+')
        self.assertTrue(candidate.charge_supported)
        self.assertEqual(text[candidate.start:candidate.end], candidate.raw)
        tracker = ErrorTracker()
        details = {}
        check_hrms(make_config(), tracker, text, [], details=details)
        self.assertEqual(details['charge'], 2)
        self.assertEqual(tracker.get_count('calcd_formula_mass_mismatch'), 0)
        self.assertEqual(len(list(iter_ion_expression_spans(text))), 1)

    def test_leading_isotopes_in_outer_formula_ions_keep_all_assertions_visible(self):
        for formula in ('[13C]C2H10O', '[18O]C3H10'):
            for charge in ('2+', '+2'):
                with self.subTest(formula=formula, charge=charge):
                    text = f'HRMS calcd for [{formula}]{charge} 38.0000, found 38.0000.'
                    candidate = find_hrms_formula_candidate(text)
                    self.assertEqual(candidate.raw, f'[{formula}]')
                    self.assertEqual(candidate.formula, formula)
                    self.assertEqual(candidate.charge_raw, charge)
                    spans = list(iter_ion_expression_spans(text))
                    self.assertEqual([text[start:end] for start, end in spans], [f'[{formula}]{charge}'])

    def test_omitted_carbon_count_in_secondary_ions_cannot_bypass_parent_qualification(self):
        first = spectrum('HRMS (ESI)', 'C3H9O')
        for ion in ('[CH5]+', '[CH5]2+', '[CCl4]+', '[[18O]CH5]+', '[[13C]H5]+'):
            with self.subTest(ion=ion):
                text = first + ion
                spans = list(iter_ion_expression_spans(text))
                self.assertEqual([text[start:end] for start, end in spans], ['[M+H]+', ion])
                result = CheckSIController(make_config())._analyze_single_description(NMR + text, 'CCCO', ErrorTracker())
                self.assertEqual(result['formula_comparisons'][-1]['status'], 'not_comparable')

    def test_unsupported_complete_formula_tokens_are_not_shortened(self):
        for token in ('[13C] C5H13O2', '2C2H6O', 'C2 H6O', 'C2H6O_opaque', 'C2H6O(H2O)', 'CH3CH2OH'):
            with self.subTest(token=token):
                text = f'HRMS calcd for {token}+ [M+H]+ 100.0000, found 100.0000.'
                candidate = find_hrms_formula_candidate(text)
                self.assertIsNotNone(candidate)
                self.assertEqual(candidate.raw, token)
                self.assertIsNone(candidate.formula)

    def test_outer_formula_charge_does_not_hide_unsupported_continuations(self):
        for formula in ('C3H10O', '[13C]C2H10O', '[18O]C3H10'):
            for suffix in ('H', ' H2O', '_opaque', '(H2O)', '·H2O'):
                with self.subTest(formula=formula, suffix=suffix):
                    token = f'[{formula}]2+{suffix}'
                    text = f'HRMS calcd for {token} 31.0362, found 31.0362.'
                    candidate = find_hrms_formula_candidate(text)
                    self.assertEqual(candidate.raw, token)
                    self.assertIsNone(candidate.formula)
                    details = {}
                    check_hrms(make_config(), ErrorTracker(), text, [], details=details)
                    self.assertFalse(details['usable'])
