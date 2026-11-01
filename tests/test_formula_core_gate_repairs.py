"""Independent core-gate edge cases and their definite-error controls."""

import unittest

from checksi.config import ConfigManager
from checksi.config_schema import ErrorSeverity
from checksi.controller import CheckSIController
from checksi.errors import ErrorCode
from checksi.analysis.formula_source_policy import parse_ea_formula
from checksi.analysis import formula_source_policy as policy
from checksi.utils.error_handler import ErrorTracker
from tests import test_formula_review_r3_core as core


class FormulaCoreGateRepairs(unittest.TestCase):
    def evaluate(self, text, smiles=None, disabled=None):
        config = ConfigManager.load_factory()
        if disabled:
            config.errors.overrides[ErrorCode(disabled)] = ErrorSeverity.DISABLED
        tracker = ErrorTracker()
        result = CheckSIController(config)._analyze_single_description(core.NMR + text, smiles, tracker)
        return result, tracker

    def pair(self, result, left='ea', right='hrms_neutral'):
        return next(item for item in result['formula_comparisons'] if item['left'] == left and item['right'] == right)

    def test_higher_kind_fractional_fold_is_pending_and_proven_typo_stays_invalid(self):
        fields = 'C, 60.46; H, 6.37; Cl, 9.15; N, 5.42; O, 14.45; S, 4.14'
        folded = 'C19.5H24.5ClN1.5O3.5S0.5'
        dotted = 'C17H19NO3·0.5C2H3N·0.5C2H6OS·0.5CH2Cl2'
        self.assertEqual(parse_ea_formula(folded).full_counts, parse_ea_formula(dotted).full_counts)
        counts = parse_ea_formula('C2.1H26N2').full_counts
        invariant = policy._fractional_impossibility_invariant(counts)
        self.assertIsNotNone(invariant)
        self.assertEqual({key: abs(value) for key, value in invariant.items()}, {'C': 8, 'H': 7, 'N': 5})
        for component in ('C2H3N', 'C7H8'):
            atoms = parse_ea_formula(component).full_counts
            self.assertEqual(sum(value * atoms.get(key, 0) for key, value in invariant.items()), 0)
        self.assertNotEqual(sum(value * counts[key] for key, value in invariant.items()).denominator, 1)
        self.assertIsNone(policy._fractional_impossibility_invariant(parse_ea_formula(folded).full_counts))
        for disabled in (None, 'formula_invalid_format'):
            for formula, supported in ((folded, False), (dotted, True)):
                with self.subTest(formula=formula, disabled=disabled):
                    text = core.ion_line('[M+H]+', 'C17H20NO3') + f'Anal. Calcd for {formula}: {fields}. Found: {fields}.'
                    result, tracker = self.evaluate(text, disabled=disabled)
                    self.assertEqual(tracker.get_count('formula_invalid_format'), 0)
                    self.assertEqual(result['formula_sources']['ea']['usable'], supported)
                    self.assertEqual(parse_ea_formula(formula).status, 'accepted' if supported else 'unsupported')
                    if supported:
                        self.assertEqual(self.pair(result)['status'], 'agree')
                        self.assertEqual(tracker.get_count('ea_formula_calcd_mismatch'), 0)
                    else:
                        self.assertTrue(any('not checked by CheckSI' in item for item in result['warnings']))
            result, tracker = self.evaluate('Anal. Calcd for C2.1H26N2: C, 31.75; H, 32.99; N, 35.26. Found: C, 31.75; H, 32.99; N, 35.26.', disabled=disabled)
            self.assertEqual(parse_ea_formula('C2.1H26N2').status, 'typo')
            self.assertEqual(result['formula_sources']['ea']['status'], 'invalid')
            self.assertEqual(tracker.get_count('formula_invalid_format'), int(disabled is None))

    def test_balanced_mixed_anions_are_pending_and_unbalanced_counts_disagree(self):
        for disabled in (None, 'formula_source_mismatch'):
            for formula, charge, status in (
                    ('C4H14Cl2N2', 2, 'agree'), ('C4H14Br2N2', 2, 'agree'),
                    ('C4H14BrClN2', 2, 'not_comparable'), ('C4H14BF10N2P', 2, 'not_comparable'),
                    ('C4H14ClN2', 2, 'disagree'), ('C4H14BrCl2N2', 2, 'disagree'),
                    ('C4H14BrClIN2', 3, 'not_comparable'), ('C4H14BrClI2N2', 3, 'disagree')):
                with self.subTest(formula=formula, disabled=disabled):
                    result, tracker = self.evaluate(core.ion_line(f'[M]{charge}+', 'C4H14N2', charge) + core.ea_line(formula), disabled=disabled)
                    self.assertEqual(self.pair(result)['status'], status)
                    self.assertEqual(tracker.get_count('formula_source_mismatch'), int(status == 'disagree' and disabled is None))
                    self.assertEqual(tracker.get_count('formula_source_review_required'), int(status == 'not_comparable'))

    def test_mixed_rare_potassium_stock_is_pending_but_one_atom_is_insufficient(self):
        for disabled in (None, 'hrms_ion_balance_invalid'):
            for formula, status in (
                    ('C2H6[39K]2O', 'recovered'), ('C2H6[39K][41K]O', 'unavailable'),
                    ('C2H6[41K]2O', 'unavailable'), ('C2H6[39K]O', 'invalid'), ('C2H6[41K]O', 'invalid')):
                with self.subTest(formula=formula, disabled=disabled):
                    result, tracker = self.evaluate(core.ion_line('[M+2K]2+', formula, 2), 'CCO', disabled)
                    parent = result['formula_sources']['hrms_neutral']
                    self.assertEqual(parent['status'], status)
                    self.assertEqual(tracker.get_count('hrms_ion_balance_invalid'), int(status == 'invalid' and disabled is None))
                    self.assertEqual(tracker.get_count('formula_source_review_required'), int(status == 'unavailable'))
                    if status == 'recovered':
                        self.assertEqual(parent['composition']['counts'], {'C': 2, 'H': 6, 'O': 1})
                    else:
                        self.assertIsNone(parent['formula'])
            result, tracker = self.evaluate(core.ion_line('[M+2H]2+', 'C2[2H]2O', 2), disabled=disabled)
            self.assertEqual(result['formula_sources']['hrms_neutral']['status'], 'invalid')
            self.assertEqual(tracker.get_count('hrms_ion_balance_invalid'), int(disabled is None))

    def test_isotope_percentage_review_does_not_hide_absent_elements(self):
        for disabled in (None, 'ea_formula_calcd_mismatch'):
            for fields, identity_error, percentage_error in (
                    ('C, 66.91; H, 7.93; O, 25.17', False, False),
                    ('C, 50.00; H, 7.93; O, 25.17', False, True),
                    ('C, 66.91; H, 7.93; N, 10.00; O, 25.17', True, False),
                    ('C, 50.00; H, 7.93; N, 10.00; O, 25.17', True, True),
                    ('13C, 66.91; H, 7.93; O, 25.17', False, True)):
                with self.subTest(fields=fields, disabled=disabled):
                    result, tracker = self.evaluate(f'Anal. Calcd for C6H10[13C]O2: {fields}. Found: {fields}.', disabled=disabled)
                    self.assertEqual(tracker.get_count('ea_formula_calcd_mismatch'), int(identity_error and disabled is None))
                    self.assertEqual(tracker.get_count('ea_isotope_calcd_mismatch'), int(percentage_error))
                    self.assertEqual(tracker.get_count('ea_formula_element_missing_in_calcd'), 0)
                    self.assertNotIn('N', result['formula_sources']['ea']['composition']['counts'])
            fields = 'C, 66.65; H, 5.92; D, 1.97; N, 4.57; O, 20.89'
            _, tracker = self.evaluate(f'Anal. Calcd for C17H18D3NO4: {fields}. Found: {fields}.', disabled=disabled)
            self.assertEqual(tracker.get_count('ea_formula_calcd_mismatch'), 0)
            self.assertEqual(tracker.get_count('ea_isotope_calcd_mismatch'), 1)

    def test_reverse_rare_isotope_adduct_basis_is_pending(self):
        for disabled in (None, 'hrms_ion_balance_invalid'):
            for ion, formula, charge, status in (
                    ('[M+[41K]]+', 'C2H6KO', 1, 'unavailable'),
                    ('[M+[41K]]+', 'C2H6[39K]O', 1, 'unavailable'),
                    ('[M+[41K]]+', 'C2H6[41K]O', 1, 'recovered'),
                    ('[M+[41K]]+', 'C2H7O', 1, 'invalid'),
                    ('[M+[41K]2]2+', 'C2H6KO', 2, 'invalid'),
                    ('[M+[2H]]+', 'C2H7O', 1, 'invalid')):
                with self.subTest(ion=ion, formula=formula, disabled=disabled):
                    result, tracker = self.evaluate(core.ion_line(ion, formula, charge), 'CCO', disabled)
                    parent = result['formula_sources']['hrms_neutral']
                    self.assertEqual(parent['status'], status)
                    self.assertEqual(tracker.get_count('hrms_ion_balance_invalid'), int(status == 'invalid' and disabled is None))
                    self.assertEqual(tracker.get_count('formula_source_review_required'), int(status == 'unavailable'))
                    if status == 'recovered':
                        self.assertEqual(parent['composition']['counts'], {'C': 2, 'H': 6, 'O': 1})
                    else:
                        self.assertIsNone(parent['formula'])

    def test_protonation_uncertainty_tracks_charge_change_without_hiding_wrong_ions(self):
        for disabled in (None, 'name_formula_mismatch'):
            for formula, charge, smiles, status in (
                    ('C4H14N2', 2, 'NCCCCN', 'not_comparable'),
                    ('C4H14N2', 2, 'NCCCC[NH3+]', 'not_comparable'),
                    ('C4H14N2', 2, '[NH3+]CCCC[NH3+]', 'agree'),
                    ('C4H14N2', 2, 'C[Si](C)(C)C', 'disagree'),
                    ('C4H13N2', 2, 'NCCCCN', 'disagree'),
                    ('C4H12N2', 2, '[NH3+]CCCC[NH3+]', 'disagree'),
                    ('C2H3O2', -1, 'CC(=O)O', 'not_comparable'),
                    ('C2H5O2', -1, 'CC(=O)O', 'disagree')):
                with self.subTest(formula=formula, charge=charge, smiles=smiles, disabled=disabled):
                    sign = '+' if charge > 0 else '-'
                    magnitude = str(abs(charge)) if abs(charge) != 1 else ''
                    result, tracker = self.evaluate(core.ion_line(f'[M]{magnitude}{sign}', formula, charge), smiles, disabled)
                    self.assertEqual(self.pair(result, 'hrms_neutral', 'name')['status'], status)
                    self.assertEqual(tracker.get_count('name_formula_mismatch'), int(status == 'disagree' and disabled is None))
                    self.assertEqual(tracker.get_count('formula_source_review_required'), int(status == 'not_comparable'))


if __name__ == '__main__':
    unittest.main()
