"""Application-level cases from Pavel's explicit 2026-10-26 decisions."""

import unittest

from checksi.controller import CheckSIController
from checksi.utils.error_handler import ErrorTracker
from tests.test_hrms import make_config
from tests.test_formula_reconciliation import NMR
from tests.test_review_wave3_formula_repairs import hrms


def ea(formula, values):
    fields = '; '.join(f'{element}, {value:.2f}' for element, value in values.items())
    return f'Anal. Calcd for {formula}: {fields}. Found: {fields}.'


class PavelFormulaPolicyIntegration(unittest.TestCase):
    def evaluate(self, text, smiles=None, overrides=None):
        tracker = ErrorTracker()
        result = CheckSIController(make_config(overrides=overrides))._analyze_single_description(
            NMR + text, smiles, tracker)
        return result, tracker

    def test_folded_and_dot_acid_salts_agree_without_a_critical_source_error(self):
        for formula in ('C21H30Cl2N2', 'C21H28N2·2HCl'):
            with self.subTest(formula=formula):
                result, tracker = self.evaluate(hrms('[M+H]+', 'C21H29N2') +
                    ea(formula, {'C': 66.13, 'H': 7.93, 'N': 7.35, 'Cl': 18.59}))
                self.assertEqual(tracker.get_count('formula_source_mismatch'), 0)
                self.assertEqual(tracker.get_count('formula_source_review_required'), 0)
                self.assertEqual(tracker.get_count('ea_formula_calcd_mismatch'), 0)
                pair = next(p for p in result['formula_comparisons'] if p['left'] == 'ea' and p['right'] == 'hrms_neutral')
                self.assertEqual(pair['status'], 'agree')
                self.assertTrue(any('HCl' in line and 'EA' in line for line in result['warnings']))

    def test_fractional_solvates_check_the_full_percentages_against_actual_hrms_parent(self):
        cases = [('C17H19.4NO3.2', 'C17H20NO3', {'C': 70.67, 'H': 6.77, 'N': 4.85, 'O': 17.72}),
                 ('C10H13O2.5', 'C10H13O2', {'C': 69.34, 'H': 7.56, 'O': 23.09})]
        for formula, ion, values in cases:
            with self.subTest(formula=formula):
                result, tracker = self.evaluate(hrms('[M+H]+', ion) + ea(formula, values))
                self.assertTrue(result['formula_sources']['ea']['usable'])
                for code in ('formula_invalid_format', 'formula_atom_symbol_invalid',
                             'formula_source_mismatch', 'ea_formula_calcd_mismatch'):
                    self.assertEqual(tracker.get_count(code), 0, code)
                wrong = dict(values)
                wrong['C'] -= 2
                _, changed = self.evaluate(hrms('[M+H]+', ion) + ea(formula, wrong))
                self.assertGreater(changed.get_count('ea_formula_calcd_mismatch'), 0)

    def test_unexplained_fractional_carbon_remains_invalid(self):
        _, tracker = self.evaluate(ea('C2.1H26N2', {'C': 31.75, 'H': 32.99, 'N': 35.26}))
        self.assertEqual(tracker.get_count('formula_invalid_format'), 1)

    def test_integer_wrong_formula_is_not_mistaken_for_a_solvate(self):
        _, tracker = self.evaluate(hrms('[M+H]+', 'C21H29N2') +
                                  ea('C22H30N2', {'C': 81.94, 'H': 9.38, 'N': 8.69}))
        self.assertEqual(tracker.get_count('formula_source_mismatch'), 1)

    def test_permanent_cation_with_balanced_counterion_is_not_called_neutral(self):
        result, tracker = self.evaluate(hrms('[M]+', 'C4H12N') +
            ea('C4H12ClN', {'C': 43.84, 'H': 11.04, 'N': 12.78, 'Cl': 32.34}), 'C[N+](C)(C)C')
        self.assertEqual(tracker.get_count('formula_source_mismatch'), 0)
        self.assertEqual(tracker.get_count('name_formula_mismatch'), 0)
        self.assertEqual(tracker.get_count('formula_source_review_required'), 0)
        self.assertEqual(result['formula_sources']['hrms_neutral']['composition']['state'], 'ion')
        self.assertTrue(any('Cl' in line and 'counter' in line.lower() for line in result['warnings']))

    def test_most_abundant_peak_labels_no_longer_force_self_contradictory_mismatches(self):
        for formula, smiles in (('C1H6[11B]O2', 'CB(O)O'), ('C1H4[19F]', 'CF'),
                                 ('C2H7[80Se]', 'C[Se]C'), ('C4H13[120Sn]', 'C[Sn](C)(C)C')):
            with self.subTest(formula=formula):
                _, tracker = self.evaluate(hrms('[M+H]+', formula), smiles)
                self.assertEqual(tracker.get_count('name_formula_mismatch'), 0)
                self.assertEqual(tracker.get_count('formula_source_review_required'), 0)

    def test_name_side_natural_label_is_isotopic_modification_not_erased(self):
        for formula, smiles in (('C2H6Cl', 'CC[37Cl]'), ('C2H6Br', 'CC[81Br]')):
            with self.subTest(smiles=smiles):
                result, tracker = self.evaluate(hrms('[M+H]+', formula), smiles)
                self.assertEqual(result['formula_comparisons'][-1]['status'], 'not_comparable')
                self.assertEqual(tracker.get_count('name_formula_mismatch'), 0)
                self.assertEqual(tracker.get_count('formula_source_review_required'), 1)

    def test_dot_counterion_does_not_reclassify_permanent_cation_as_radical(self):
        result, tracker = self.evaluate(hrms('[M]+', 'C4H12N') +
            ea('C4H12N·Cl', {'C': 43.84, 'H': 11.04, 'N': 12.78, 'Cl': 32.34}), 'C[N+](C)(C)C')
        self.assertEqual(result['formula_sources']['hrms_neutral']['composition']['state'], 'ion')
        self.assertEqual(tracker.get_count('formula_source_mismatch'), 0)
        self.assertEqual(tracker.get_count('formula_source_review_required'), 0)

    def test_uncovered_fractional_notation_is_informational_not_an_author_error(self):
        result, tracker = self.evaluate(ea('C3H8O2.5H2O', {'C': 40.00, 'H': 10.00}))
        self.assertFalse(result['formula_sources']['ea']['usable'])
        self.assertEqual(tracker.get_count('formula_invalid_format'), 0)
        self.assertTrue(any('not checked by CheckSI' in item for item in result['warnings']))

    def test_explicit_dot_parent_is_not_replaced_by_an_alternative_solvent(self):
        declaration = ea('C3H8O·H2O', {'C': 46.13, 'H': 12.90, 'O': 40.97})
        for ion_formula, smiles, expected_source, expected_name in (
                ('C2H7O', 'CCO', 1, 0), (None, 'CCO', 0, 1),
                ('C3H11O2', None, 1, 0), ('C3H9O', 'CCCO', 0, 0)):
            with self.subTest(ion_formula=ion_formula, smiles=smiles):
                text = (hrms('[M+H]+', ion_formula) if ion_formula else '') + declaration
                result, tracker = self.evaluate(text, smiles)
                self.assertEqual(tracker.get_count('formula_source_mismatch'), expected_source)
                self.assertEqual(tracker.get_count('name_formula_mismatch'), expected_name)
                if expected_source:
                    self.assertEqual(result['formula_comparisons'][0]['status'], 'disagree')

    def test_natural_sodium_and_potassium_peak_labels_keep_element_identity(self):
        for ion, formula in (('[M+Na]+', 'C2H6[23Na]O'), ('[M+K]+', 'C2H6[39K]O')):
            with self.subTest(ion=ion):
                result, tracker = self.evaluate(hrms(ion, formula), 'CCO')
                self.assertEqual(tracker.get_count('hrms_adduct_mismatch'), 0)
                self.assertEqual(tracker.get_count('name_formula_mismatch'), 0)
                self.assertEqual(result['formula_sources']['hrms_neutral']['formula'], 'C2H6O')
