"""Charge authority, conventional adducts and m/z from Pavel answer 10."""

import unittest

from molmass import Formula

from checksi.analysis.hrms import check_hrms
from checksi.controller import CheckSIController
from checksi.utils.error_handler import ErrorTracker
from tests.test_formula_reconciliation import NMR
from tests.test_hrms import make_config


def line(ion, formula, suffix, charge):
    mz = (Formula(formula).isotope.mass - charge * 0.000548579909065) / abs(charge)
    return f'HRMS {ion} calcd for {formula}{suffix} {mz:.4f}, found {mz:.4f}. '


class PavelChargePolicy(unittest.TestCase):
    def test_each_missing_written_charge_is_reported_independently_of_inference(self):
        for ion, suffix in (('[M+H]', '+'), ('[M+H]+', ''), ('[M+H]', '')):
            with self.subTest(ion=ion, suffix=suffix):
                tracker = ErrorTracker()
                check_hrms(make_config(), tracker, line(ion, 'C2H7O', suffix, 1), [])
                self.assertEqual(tracker.get_count('hrms_formula_charge_missing'), int(not suffix))
                self.assertEqual(tracker.get_count('hrms_ion_charge_missing'), int(ion == '[M+H]'))
                self.assertEqual(tracker.get_count('calcd_formula_mass_mismatch'), 0)

    def test_both_missing_signs_are_reported_but_conflicting_charges_are_reported_once(self):
        tracker = ErrorTracker()
        check_hrms(make_config(), tracker, 'HRMS [M] calcd for C2H6O 46.0419, found 46.0419.', [])
        self.assertEqual(tracker.get_count('hrms_formula_charge_missing') +
                         tracker.get_count('hrms_ion_charge_missing'), 2)
        tracker = ErrorTracker()
        check_hrms(make_config(), tracker, line('[M+H]-', 'C2H7O', '+', 1), [])
        self.assertEqual(tracker.get_count('hrms_charge_inconsistent'), 1)
        self.assertEqual(tracker.get_count('calcd_formula_mass_mismatch'), 0)

    def test_declared_double_charges_use_mz_and_recover_neutral_atoms(self):
        for ion, formula in (('[M+2H]2+', 'C3H10O2'), ('[M+H+Na]2+', 'C3H9NaO2')):
            with self.subTest(ion=ion):
                tracker = ErrorTracker()
                text = line(ion, formula, '+2', 2)
                result = CheckSIController(make_config())._analyze_single_description(NMR + text, 'OCCCO', tracker)
                self.assertEqual(tracker.get_count('calcd_formula_mass_mismatch'), 0)
                self.assertEqual(tracker.get_count('hrms_charge_inconsistent'), 0)
                self.assertEqual(tracker.get_count('formula_source_review_required'), 0)
                self.assertEqual(result['formula_sources']['hrms_neutral']['composition']['counts'], {'C': 3, 'H': 8, 'O': 2})

    def test_extended_conventional_adducts_supply_charge_without_duplicate_review(self):
        for ion, formula, charge in (('[M+Cs]', 'C2H6CsO', 1), ('[M+Ag]', 'C2H6AgO', 1),
                                     ('[M+Cl]', 'C2H6ClO', -1), ('[M+HCOO]', 'C3H7O3', -1),
                                     ('[M+CH3COO]', 'C4H9O3', -1),
                                     ('[M+H-H2O]', 'C2H5', 1)):
            with self.subTest(ion=ion):
                tracker = ErrorTracker()
                text = line(ion, formula, '', charge)
                result = CheckSIController(make_config())._analyze_single_description(NMR + text, 'CCO', tracker)
                self.assertEqual(tracker.get_count('hrms_formula_charge_missing'), 1)
                self.assertEqual(tracker.get_count('hrms_ion_charge_missing'), 1)
                for code in ('formula_source_review_required', 'calcd_formula_mass_mismatch'):
                    self.assertEqual(tracker.get_count(code), 0, code)
                self.assertEqual(result['formula_sources']['hrms_neutral']['composition']['counts'], {'C': 2, 'H': 6, 'O': 1})

    def test_formula_charge_conflict_has_one_diagnostic_and_no_generic_review(self):
        for ion, suffix, charge in (('[M+H]-', '+', 1), ('[M+H]+', '-', -1),
                                    ('[M+H]+', '+2', 2)):
            for severity in ('disabled', 'info', 'warning', 'critical'):
                with self.subTest(ion=ion, suffix=suffix, severity=severity):
                    tracker = ErrorTracker()
                    cfg = make_config(overrides={'hrms_charge_inconsistent': severity})
                    result = CheckSIController(cfg)._analyze_single_description(
                        NMR + line(ion, 'C2H7O', suffix, charge), 'CCO', tracker)
                    self.assertEqual(tracker.get_count('hrms_charge_inconsistent'), int(severity != 'disabled'))
                    for code in ('hrms_formula_charge_missing', 'hrms_ion_charge_missing',
                                 'formula_source_review_required', 'formula_source_mismatch',
                                 'name_formula_mismatch', 'calcd_formula_mass_mismatch'):
                        self.assertEqual(tracker.get_count(code), 0, code)
                    self.assertEqual(result['formula_comparisons'][-1]['status'], 'not_comparable')

    def test_unsupported_charge_keeps_review_instead_of_certifying_same_counts(self):
        tracker = ErrorTracker()
        result = CheckSIController(make_config())._analyze_single_description(
            NMR + line('[M+H]+', 'C2H7O', '++', 1), 'CCO', tracker)
        self.assertEqual(tracker.get_count('formula_source_review_required'), 1)
        self.assertEqual(result['formula_comparisons'][-1]['status'], 'not_comparable')

    def test_every_owned_ion_requires_consistent_explicit_charges(self):
        first = line('[M+H]+', 'C2H7O', '+', 1)
        for second_ion, expected in (('[M+Na]+', 'agree'), ('[M+Na]-', 'not_comparable')):
            tracker = ErrorTracker()
            text = first + line(second_ion, 'C2H6NaO', '+', 1).removeprefix('HRMS ')
            result = CheckSIController(make_config())._analyze_single_description(NMR + text, 'CCO', tracker)
            self.assertEqual(result['formula_comparisons'][-1]['status'], expected)
            self.assertEqual(tracker.get_count('formula_source_review_required'), int(expected == 'not_comparable'))

    def test_original_ion_composition_retains_the_selected_charge(self):
        for ion, formula, suffix, charge in (('[M+H]+', 'C2H7O', '+', 1),
                ('[M-H]-', 'C2H5O', '-', -1), ('[M+2H]2+', 'C3H10O2', '+2', 2)):
            for level in ('disabled', 'info', 'critical'):
                tracker = ErrorTracker()
                cfg = make_config(overrides={'hrms_charge_inconsistent': level,
                                              'formula_source_review_required': level})
                result = CheckSIController(cfg)._analyze_single_description(
                    NMR + line(ion, formula, suffix, charge), None, tracker)
                source = result['formula_sources']['hrms']
                self.assertEqual(source['charge'], charge)
                self.assertEqual(source['composition']['charge'], charge)
        result = CheckSIController(make_config())._analyze_single_description(
            NMR + 'HRMS [M] calcd for C2H6O 46.0419, found 46.0419.', None, ErrorTracker())
        self.assertIsNone(result['formula_sources']['hrms']['charge'])
        self.assertIsNone(result['formula_sources']['hrms']['composition']['charge'])
