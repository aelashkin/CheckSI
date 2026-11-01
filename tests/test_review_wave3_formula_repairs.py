"""Reported adduct and source-selection regressions without external services."""

import unittest

from molmass import Formula

from checksi.analysis.formula_isotopes import replace_common_isotope_formats
from checksi.controller import CheckSIController
from checksi.utils.error_handler import ErrorTracker
from tests.test_formula_reconciliation import EA_ETHANOL, EA_PROPANOL, HRMS, NMR
from tests.test_hrms import make_config


def hrms(ion, formula, charge='+'):
    electron = -0.000548579909065 if charge == '+' else 0.000548579909065
    mass = Formula(formula).monoisotopic_mass + electron
    return f'HRMS {ion} calcd for {formula}{charge} {mass:.4f}, found {mass:.4f}. '


class AdductAndSelectionRepairs(unittest.TestCase):
    def evaluate(self, block, smiles, overrides=None):
        tracker = ErrorTracker()
        result = CheckSIController(make_config(overrides=overrides))._analyze_single_description(
            NMR + block, smiles, tracker)
        return result, tracker

    def test_stoichiometric_adduct_coefficients_do_not_create_deuterium(self):
        for ion, formula, charge in (('[M+H-2H2O]+', 'C3H5', '+'),
                                      ('[M-2H+Na]-', 'C3H6NaO2', '-'),
                                      ('[M+K-2H]-', 'C3H6KO2', '-')):
            for severity in ('disabled', 'warning'):
                with self.subTest(ion=ion, severity=severity):
                    result, tracker = self.evaluate(hrms(ion, formula, charge), 'OCCCO',
                        {'name_formula_mismatch': severity, 'formula_source_mismatch': severity})
                    source = result['formula_sources']['hrms_neutral']
                    self.assertEqual(source['composition']['counts'], {'C': 3, 'H': 8, 'O': 2})
                    self.assertNotIn('2H', source['formula'])
                    self.assertEqual(tracker.get_count('name_formula_mismatch'), 0)
                    self.assertEqual(tracker.get_count('formula_source_mismatch'), 0)

    def test_explicit_isotopic_adduct_is_not_reinterpreted_as_a_coefficient(self):
        result, _ = self.evaluate(hrms('[M+[2H]]+', 'C2H6[2H]O'), 'CCO')
        self.assertEqual(result['formula_sources']['hrms_neutral']['composition']['counts'],
                         {'C': 2, 'H': 6, 'O': 1})
        self.assertEqual(result['formula_sources']['hrms']['composition']['counts']['2H'], 1)

    def test_name_uses_comparable_ea_when_hrms_basis_is_uncertifiable(self):
        cases = (HRMS.replace('C3H9O+', 'C3H9O+2'),
                 HRMS.replace('[M+H]+', '[M+H]+ [M+Na]+'))
        for block in cases:
            with self.subTest(block=block, mismatch='ea'):
                result, tracker = self.evaluate(block + EA_ETHANOL, 'CCCO')
                self.assertFalse(result['formula_sources']['hrms_neutral']['comparable'])
                self.assertEqual(tracker.get_count('name_formula_mismatch'), 1)
                warning, = [line for line in result['warnings'] if 'name_formula_mismatch.' in line]
                self.assertIn('C2H6O', warning)
            with self.subTest(block=block, mismatch='name'):
                _, tracker = self.evaluate(block + EA_PROPANOL, 'CCO')
                self.assertEqual(tracker.get_count('name_formula_mismatch'), 1)

    def test_reviewed_natural_adduct_labels_are_consumed_without_losing_parent_labels(self):
        for ion, formula, charge in (('[M+Cl]-', 'C2H6[35Cl]O', '-'),
                                      ('[M+Br]-', 'C2H6[79Br]O', '-'),
                                      ('[M+NH4]+', 'C2H10[14N]O', '+')):
            for severity in ('disabled', 'critical'):
                with self.subTest(ion=ion, severity=severity):
                    result, tracker = self.evaluate(hrms(ion, formula, charge), 'CCO',
                                                    {'hrms_ion_balance_invalid': severity})
                    self.assertEqual(result['formula_sources']['hrms_neutral']['composition']['counts'],
                                     {'C': 2, 'H': 6, 'O': 1})
                    self.assertEqual(tracker.get_count('hrms_ion_balance_invalid'), 0)
                    self.assertEqual(tracker.get_count('name_formula_mismatch'), 0)

    def test_inline_deuterium_then_halogen_labels_keep_separate_atom_counts(self):
        normalized = replace_common_isotope_formats('C19H232H35Cl2N3')
        self.assertEqual(normalized, 'C19H23[2H][35Cl]2N3')
        composition = Formula(normalized).composition()
        self.assertEqual(composition['2H'].count, 1)
        self.assertEqual(composition['35Cl'].count, 2)
