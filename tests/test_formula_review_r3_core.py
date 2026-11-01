"""Public chemistry regressions for the third formula-source review."""

import json
import os
import re
import subprocess
import sys
import unittest

from molmass import Formula
from rdkit import Chem

from checksi.analysis.cross_checks import compare_name_and_formula
from checksi.analysis.elemental import calculate_elemental_percentages
from checksi.analysis.formula_source_policy import (
    _whole_three_amounts, explain_formula_difference, has_three_kind_component_difference,
    natural_isotope_eligibility, parse_ea_formula,
)
from checksi.analysis.hrms_fields import find_ea_formula_candidate
from checksi.controller import CheckSIController
from checksi.config import ConfigManager
from checksi.config_schema import ErrorSeverity
from checksi.errors.findings import Finding
from checksi.report_rendering import _formula_html
from checksi.utils.error_handler import ErrorTracker
from tests.test_formula_reconciliation import NMR
from tests.test_hrms import make_config


def ion_line(ion, formula, charge=1, header='HRMS (ESI)'):
    mass = (Formula(formula).isotope.mass - charge * 0.000548579909065) / abs(charge)
    suffix = '+' if charge == 1 else '-' if charge == -1 else f'{charge:+d}'
    return f'{header} {ion} calcd for {formula}{suffix} {mass:.4f}, found {mass:.4f}. '


def ea_line(formula, inventory=None, carbon_offset=0):
    # Independent molmass composition provides published values for integer
    # inventories; explicit components are passed as their expanded inventory.
    masses = {}
    for key, entry in Formula(inventory or formula).composition().items():
        symbol = key.lstrip('0123456789')
        masses[symbol] = masses.get(symbol, 0) + entry.fraction * 100
    fields = '; '.join(f'{symbol}, {value:.2f}' for symbol, value in masses.items())
    found = '; '.join(f'{symbol}, {value + (carbon_offset if symbol == "C" else 0):.2f}'
                      for symbol, value in masses.items())
    return f'Anal. Calcd for {formula}: {fields}. Found: {found}. '


class FormulaReviewR3CoreTests(unittest.TestCase):
    def evaluate(self, text, smiles=None):
        tracker = ErrorTracker()
        result = CheckSIController(make_config())._analyze_single_description(NMR + text, smiles, tracker)
        return result, tracker

    def pair(self, result, left='ea', right='hrms_neutral'):
        return next(pair for pair in result['formula_comparisons'] if pair['left'] == left and pair['right'] == right)

    def test_component_subsets_cannot_hide_wrong_ea_or_wrong_name(self):
        for formula in ('C22H30N2O', 'C23H30N2O', 'C26H36N2O2', 'C22H31ClN2', 'C21H30N2O2'):
            with self.subTest(formula=formula):
                result, tracker = self.evaluate(ion_line('[M+H]+', 'C21H29N2') + ea_line(formula))
                self.assertEqual(self.pair(result)['status'], 'disagree')
                self.assertEqual(tracker.get_count('formula_source_mismatch'), 1)
                self.assertEqual(tracker.get_count('ea_formula_calcd_mismatch'), 0)
        _, tracker = self.evaluate(ea_line('C4H10O2'), 'CCCO')
        self.assertEqual(tracker.get_count('name_formula_mismatch'), 1)

    def test_exact_three_kind_fold_is_pending_and_dot_parent_is_authoritative(self):
        folded = 'C22H35ClN2O2'
        parent = parse_ea_formula('C21H28N2').full_counts
        self.assertFalse(explain_formula_difference(parse_ea_formula(folded).full_counts, parent).recognized)
        result, tracker = self.evaluate(ion_line('[M+H]+', 'C21H29N2') + ea_line(folded))
        self.assertEqual(self.pair(result)['status'], 'not_comparable')
        self.assertTrue(self.pair(result)['review_required'])
        self.assertEqual(tracker.get_count('formula_source_review_required'), 1)
        explicit, tracker = self.evaluate(ion_line('[M+H]+', 'C21H29N2') +
            ea_line('C21H28N2·HCl·H2O·CH4O', folded))
        self.assertEqual(self.pair(explicit)['status'], 'agree')
        self.assertEqual(tracker.get_count('formula_source_review_required'), 0)

    def test_three_kind_pending_detection_has_no_small_coefficient_cutoff(self):
        parent = parse_ea_formula('C21H28N2').full_counts
        for formula in ('C24H56N2O11S', 'C24H2000038N2O1000002S', 'C28H48N2O3'):
            with self.subTest(formula=formula):
                counts = parse_ea_formula(formula).full_counts
                self.assertTrue(has_three_kind_component_difference(counts, parent))
                self.assertFalse(explain_formula_difference(counts, parent).recognized)
                result, tracker = self.evaluate(ion_line('[M+H]+', 'C21H29N2') + ea_line(formula))
                self.assertEqual(self.pair(result)['status'], 'not_comparable')
                self.assertEqual(tracker.get_count('formula_source_review_required'), 1)
                self.assertEqual(tracker.get_count('formula_source_mismatch'), 0)
        self.assertFalse(has_three_kind_component_difference(parse_ea_formula('C22H30N2O').full_counts, parent))

    def test_rank_two_component_coefficients_must_be_positive_whole_numbers(self):
        # Water, ethanol and ether are rank two. Their even carbon counts make
        # the odd-carbon delta impossible despite a positive rational solution.
        parts = tuple(parse_ea_formula(formula).full_counts for formula in ('H2O', 'C2H6O', 'C4H10O'))
        self.assertTrue(_whole_three_amounts(parse_ea_formula('C6H18O3').full_counts, parts))
        self.assertFalse(_whole_three_amounts(parse_ea_formula('C5H16O3').full_counts, parts))
        self.assertFalse(_whole_three_amounts(parse_ea_formula('C9H28O5').full_counts, parts))
        self.assertFalse(_whole_three_amounts(parse_ea_formula('C2H8O2').full_counts, parts))

    def test_ei_and_raw_radical_spellings_compare_matching_and_wrong_sources(self):
        forms = [('EI-HRMS', '[M]+'), ('EI HRMS', '[M]+'), ('EI-TOF', '[M]+'),
                 ('HRMS (EI)', '[M]+'), ('HRMS (APCI)', '[M•]+'), ('HRMS (APCI)', '[M+•]'),
                 ('HRMS (APCI)', '(M)+•'), ('HRMS (APCI)', '(M)•+'),
                 ('HRMS (APCI)', '[M]+•'), ('HRMS (APCI)', '[M]•+')]
        for header, ion in forms:
            with self.subTest(header=header, ion=ion):
                line = ion_line(ion, 'C14H10', header=header)
                result, tracker = self.evaluate(line + ea_line('C14H10'), 'c1ccc2cc3ccccc3cc2c1')
                self.assertEqual(result['formula_sources']['hrms_neutral']['composition']['state'], 'neutral')
                self.assertEqual(self.pair(result)['status'], 'agree')
                self.assertEqual(tracker.get_count('name_formula_mismatch'), 0)
                _, wrong = self.evaluate(line + ea_line('C14H12'), 'c1ccc2ccccc2c1')
                self.assertEqual(wrong.get_count('formula_source_mismatch'), 1)
                self.assertEqual(wrong.get_count('name_formula_mismatch'), 1)

    def test_permanent_ion_names_with_different_atoms_remain_wrong(self):
        for ion, formula, charge, correct, wrong in (
                ('[M]+', 'C4H12N', 1, 'C[N+](C)(C)C', 'CN(C)N(C)C'),
                ('[M]+', 'C4H12N', 1, 'C[N+](C)(C)C', 'C[N+](C)(C)CO'),
                ('[M]2+', 'C4H14N2', 2, 'C[NH2+]CCC[NH3+]', 'C[NH2+]CCOC[NH3+]'),
                ('[M]-', 'C2H3O2', -1, 'CC(=O)[O-]', 'CC(=S)[O-]')):
            with self.subTest(ion=ion, wrong=wrong):
                _, tracker = self.evaluate(ion_line(ion, formula, charge), wrong)
                self.assertEqual(tracker.get_count('name_formula_mismatch'), 1)
                result, tracker = self.evaluate(ion_line(ion, formula, charge), correct)
                self.assertEqual(self.pair(result, 'hrms_neutral', 'name')['status'], 'agree')
                self.assertEqual(tracker.get_count('name_formula_mismatch'), 0)

    def test_wrong_permanent_cation_ea_and_wrong_dot_cation_are_visible(self):
        for formula in ('C4H10ClN', 'C3H10ClN', 'C5H14ClN', 'C4H12N', 'C4H12Cl2N', 'C4H12BrClN'):
            with self.subTest(formula=formula):
                result, tracker = self.evaluate(ion_line('[M]+', 'C4H12N') + ea_line(formula))
                self.assertEqual(self.pair(result)['status'], 'disagree')
                self.assertEqual(tracker.get_count('formula_source_mismatch'), 1)
                warning, = [item for item in result['warnings'] if 'formula_source_mismatch.' in item]
                self.assertIn('(parent ion C4H12N)', warning)
                self.assertNotIn('(neutral ', warning)
        result, tracker = self.evaluate(ion_line('[M]2+', 'C4H14N2', 2) + ea_line('C4H14ClN2'))
        self.assertEqual(self.pair(result)['status'], 'disagree')
        for reference, smiles in ((ion_line('[M+H]+', 'C4H12N'), None), ('', 'CCCCN')):
            result, tracker = self.evaluate(reference + ea_line('C3H10N·Cl', 'C3H10ClN'), smiles)
            self.assertEqual(self.pair(result, 'ea', 'name' if smiles else 'hrms_neutral')['status'], 'disagree')
            self.assertEqual(tracker.get_count('name_formula_mismatch' if smiles else 'formula_source_mismatch'), 1)
        result, tracker = self.evaluate(ion_line('[M]+', 'C4H12N', header='HRMS (EI)') +
                                        ea_line('C4H12N·Cl', 'C4H12ClN'))
        self.assertEqual(self.pair(result)['status'], 'not_comparable')
        self.assertEqual(tracker.get_count('formula_source_review_required'), 1)
        self.assertEqual(tracker.get_count('formula_source_mismatch'), 0)

    def test_fractional_multiple_components_are_pending_and_typo_stays_critical(self):
        for formula in ('C17.25H20.5Cl0.5NO3.5', 'C21H29.5Cl1.5N2', 'C22H28.5F1.5N2O'):
            with self.subTest(formula=formula):
                self.assertEqual(parse_ea_formula(formula).status, 'unsupported')
                result, tracker = self.evaluate(f'Anal. Calcd for {formula}: C, 50.00. Found: C, 50.00.')
                self.assertEqual(tracker.get_count('formula_invalid_format'), 0)
                self.assertTrue(any('not checked by CheckSI' in item for item in result['warnings']))
        self.assertEqual(parse_ea_formula('C2.1H26N2').status, 'typo')
        _, tracker = self.evaluate('Anal. Calcd for C2.1H26N2: C, 31.75; H, 32.99; N, 35.26. Found: C, 31.75; H, 32.99; N, 35.26.')
        self.assertEqual(tracker.get_count('formula_invalid_format'), 1)


    def test_labelled_ea_percentages_are_identical_for_equal_atom_inventories(self):
        for folded, dotted in (('C17H18D3NO4', 'C17H16D3NO3·H2O'),
                               ('C6H10[13C]O2', 'C6H8[13C]O·H2O')):
            self.assertEqual(calculate_elemental_percentages(folded), calculate_elemental_percentages(dotted))
            text = ea_line(dotted, folded)
            _, dotted_tracker = self.evaluate(text)
            _, folded_tracker = self.evaluate(text.replace(dotted, folded))
            self.assertEqual(dotted_tracker.get_count('ea_formula_calcd_mismatch'), 0)
            self.assertEqual(folded_tracker.get_count('ea_formula_calcd_mismatch'), 0)
            for formula in (folded, dotted):
                result, tracker = self.evaluate(ea_line(formula, folded, carbon_offset=-2))
                self.assertTrue(result['formula_sources']['ea']['usable'])
                self.assertEqual(tracker.get_count('formula_source_review_required'), 0)
                self.assertEqual(tracker.get_count('ea_tolerance_exceeded'), 1)
                self.assertEqual(tracker.get_count('ea_isotope_calcd_mismatch'), 0)
                self.assertEqual(result['formula_sources']['ea']['percentage_conventions']['interpretation'], 'pending')
                wrong_theory = re.sub(r'C, \d+\.\d+', 'C, 50.00', ea_line(formula, folded))
                _, tracker = self.evaluate(wrong_theory)
                self.assertEqual(tracker.get_count('ea_formula_calcd_mismatch'), 0)
                self.assertEqual(tracker.get_count('formula_source_review_required'), 0)
                self.assertEqual(tracker.get_count('ea_isotope_calcd_mismatch'), 1)
        _, tracker = self.evaluate(ion_line('[M+H]+', 'C6H11[13C]O2') + ea_line('C5H8[13C]O2'))
        self.assertEqual(tracker.get_count('formula_source_mismatch'), 1)

    def test_isotope_ea_accepts_mass_and_atom_count_axes_and_warns_for_neither(self):
        cfg = make_config()
        cfg.errors.default_severity = ErrorSeverity.WARNING
        controller = CheckSIController(cfg)
        cases = (
            ('C17H18D3NO4', 'C17H16D3NO3·H2O', 'H', (7.89, 6.91), {'C': 66.65, 'H': 7.89, 'N': 4.57, 'O': 20.89}),
            ('C6H10[13C]O2', 'C6H8[13C]O·H2O', 'C', (66.91, 66.12), {'C': 66.91, 'H': 7.93, 'O': 25.17}),
        )
        for folded, dotted, element, percentages, correct in cases:
            for formula in (folded, dotted):
                for value in (*percentages, 50.00):
                    with self.subTest(formula=formula, value=value):
                        values = {**correct, element: value}
                        fields = '; '.join(f'{key}, {percentage:.2f}' for key, percentage in values.items())
                        text = f'Anal. Calcd for {formula}: {fields}. Found: {fields}.'
                        tracker = ErrorTracker()
                        result = controller._analyze_description_diagnostics(NMR + text, None, tracker)
                        self.assertTrue(result['formula_sources']['ea']['usable'])
                        self.assertEqual(tracker.get_count('ea_formula_calcd_mismatch'), 0)
                        self.assertEqual(tracker.get_count('ea_isotope_calcd_mismatch'), int(value == 50.00))
                        self.assertEqual(tracker.get_count('ea_tolerance_exceeded'), 0)
                        for finding in result['warnings']:
                            if isinstance(finding, Finding) and finding.code.value == 'ea_isotope_calcd_mismatch':
                                self.assertEqual(finding.severity, ErrorSeverity.WARNING)
        mass_vector = {'C': 65.35, 'H': 10.06, 'O': 24.58}
        atom_vector = {'C': 64.59, 'H': 7.74, 'O': 24.58}
        for formula in ('C6H7D3[13C]O2', 'C6H5D3[13C]O·H2O'):
            for values, expected in ((mass_vector, 0), (atom_vector, 0), ({**mass_vector, 'H': 7.74}, 1)):
                fields = '; '.join(f'{key}, {value:.2f}' for key, value in values.items())
                tracker = ErrorTracker()
                result = controller._analyze_description_diagnostics(
                    NMR + f'Anal. Calcd for {formula}: {fields}. Found: {fields}.', None, tracker)
                self.assertEqual(tracker.get_count('ea_isotope_calcd_mismatch'), expected)
                self.assertEqual(tracker.get_count('formula_source_review_required'), 0)
                self.assertEqual(tracker.get_count('ea_formula_calcd_mismatch'), 0)


    def test_nominal_mass_offsets_do_not_become_charge_magnitudes(self):
        for ion in ('[M+2]+', '[M+H+2]+', '[M+4]+', '[M+18]+', '[M+23]+'):
            with self.subTest(ion=ion):
                result, tracker = self.evaluate(ion_line(ion, 'C3H9O'))
                self.assertEqual(result['formula_sources']['hrms']['charge'], 1)
                self.assertEqual(tracker.get_count('hrms_charge_inconsistent'), 0)
                self.assertEqual(tracker.get_count('hrms_formula_charge_missing'), 0)
                self.assertEqual(tracker.get_count('calcd_formula_mass_mismatch'), 0)
                self.assertFalse(result['formula_sources']['hrms_neutral']['comparable'])
        # Formula charge remains authoritative for m/z while one precise
        # diagnostic owns genuinely contradictory written charges.
        result, tracker = self.evaluate(ion_line('[M+H+]-', 'C3H9O'))
        self.assertEqual(tracker.get_count('hrms_charge_inconsistent'), 1)
        self.assertEqual(tracker.get_count('hrms_formula_charge_missing'), 0)
        self.assertEqual(tracker.get_count('calcd_formula_mass_mismatch'), 0)
        self.assertFalse(any('None' in item for item in result['warnings']))

    def test_inline_labelled_adducts_keep_isotope_atoms_and_stoichiometry(self):
        for ion, formula, expected in (
                ('[M+15NH4]+', 'C3H12[15N]O', {'C': 3, 'H': 8, 'O': 1}),
                ('[M-13CH3]+', 'C2H5O', {'C': 2, '13C': 1, 'H': 8, 'O': 1}),
                ('[M+H-18O]+', 'C3H9', {'C': 3, 'H': 8, '18O': 1})):
            with self.subTest(ion=ion):
                result, tracker = self.evaluate(ion_line(ion, formula))
                self.assertEqual(tracker.get_count('hrms_ion_balance_invalid'), 0)
                self.assertEqual(result['formula_sources']['hrms_neutral']['composition']['counts'], expected)

    def test_dot_ea_checks_found_values_and_name_parent(self):
        smiles = 'Cc1ccc(NCC2CCC(CNc3ccc(C)cc3)C2)cc1'
        for formula, inventory in (('C21H30Cl2N2', None), ('C21H28N2·2HCl', 'C21H30Cl2N2'),
                                   ('C21H30N2O', None), ('C21H28N2·H2O', 'C21H30N2O')):
            with self.subTest(formula=formula):
                result, tracker = self.evaluate(ea_line(formula, inventory), smiles)
                self.assertEqual(self.pair(result, 'ea', 'name')['status'], 'agree')
                self.assertEqual(tracker.get_count('name_formula_mismatch'), 0)
                self.assertEqual(tracker.get_count('name_formula_mass_delta'), 0)
        _, tracker = self.evaluate(ea_line('C21H28N2·2HCl', 'C21H30Cl2N2', -3))
        self.assertEqual(tracker.get_count('ea_tolerance_exceeded'), 1)

    def test_counterion_abbreviations_recover_complete_neutral(self):
        for ion, formula, charge, expected in (
                ('[M+OAc]-', 'C4H9O3', -1, {'C': 2, 'H': 6, 'O': 1}),
                ('[M-OTf]+', 'C4H12N', 1, {'C': 5, 'H': 12, 'N': 1, 'F': 3, 'O': 3, 'S': 1}),
                ('[M-BF4]+', 'C4H12N', 1, {'C': 4, 'H': 12, 'N': 1, 'B': 1, 'F': 4}),
                ('[M+OTf]-', 'C3H6F3O4S', -1, {'C': 2, 'H': 6, 'O': 1})):
            with self.subTest(ion=ion):
                result, tracker = self.evaluate(ion_line(ion, formula, charge))
                self.assertEqual(result['formula_sources']['hrms_neutral']['composition']['counts'], expected)
                self.assertTrue(result['formula_sources']['hrms_neutral']['comparable'])
                self.assertEqual(tracker.get_count('hrms_ion_balance_invalid'), 0)

    def test_natural_isotope_threshold_and_nonmodal_peak_mass(self):
        eligible = natural_isotope_eligibility()
        for element, isotope, smiles, formula in (('Mg', 25, 'C[Mg]C', 'C2H7[25Mg]'),
                ('Mg', 26, 'C[Mg]C', 'C2H7[26Mg]'), ('Hg', 198, 'C[Hg]C', 'C2H7[198Hg]'),
                ('Hg', 201, 'C[Hg]C', 'C2H7[201Hg]'), ('Sn', 116, 'C[Sn](C)(C)C', 'C4H13[116Sn]')):
            with self.subTest(isotope=isotope, element=element):
                self.assertIn(isotope, eligible[element])
                result, tracker = self.evaluate(ion_line('[M+H]+', formula), smiles)
                self.assertEqual(self.pair(result, 'hrms_neutral', 'name')['status'], 'agree')
                self.assertEqual(tracker.get_count('name_formula_mismatch'), 0)
                self.assertEqual(tracker.get_count('formula_source_review_required'), 0)

    def test_uncovered_isotope_and_unlisted_anion_remain_visible_pending(self):
        result, tracker = self.evaluate(ion_line('[M+H]+', 'C2H7[34S]'), 'CSC')
        self.assertEqual(self.pair(result, 'hrms_neutral', 'name')['status'], 'not_comparable')
        self.assertEqual(tracker.get_count('formula_source_review_required'), 1)
        # A listed counter-ion plus solvent is outside the cation-only salt
        # adjudication; parent O atoms must not make this a wrong-EA case.
        result, tracker = self.evaluate(ion_line('[M]+', 'C3H10NO') + ea_line('C4H14ClNO2'))
        self.assertEqual(self.pair(result)['status'], 'not_comparable')
        self.assertEqual(tracker.get_count('formula_source_review_required'), 1)
        self.assertEqual(tracker.get_count('formula_source_mismatch'), 0)
        self.assertEqual(tracker.get_count('name_formula_mismatch'), 0)
        result, tracker = self.evaluate(ion_line('[M]+', 'C4H12N') + ea_line('C4H12N2O3'))
        self.assertEqual(self.pair(result)['status'], 'not_comparable')
        self.assertTrue(self.pair(result)['review_required'])
        self.assertEqual(tracker.get_count('formula_source_review_required'), 1)

    def test_fractional_reference_qualifies_only_the_matching_parent(self):
        declaration = 'Anal. Calcd for C10H13O2.5: C, 69.34; H, 7.56; O, 23.09. Found: C, 69.34; H, 7.56; O, 23.09.'
        result, tracker = self.evaluate(ion_line('[M+H]+', 'C10H13O2') + declaration)
        self.assertEqual(self.pair(result)['status'], 'agree')
        self.assertEqual(tracker.get_count('formula_source_review_required'), 0)
        result, tracker = self.evaluate(ion_line('[M+H]+', 'C10H11O2') + declaration)
        self.assertEqual(self.pair(result)['status'], 'not_comparable')
        self.assertTrue(self.pair(result)['review_required'])
        self.assertEqual(tracker.get_count('formula_source_review_required'), 1)

    def test_secondary_unadjudicated_ion_blocks_neutral_agreement(self):
        first = ion_line('[M+H]+', 'C2H7O')
        second = ion_line('[M+CH3]+', 'C3H9O').replace('HRMS (ESI) ', '')
        result, tracker = self.evaluate(first + second, 'CCO')
        self.assertEqual(self.pair(result, 'hrms_neutral', 'name')['status'], 'not_comparable')
        self.assertEqual(tracker.get_count('formula_source_review_required'), 1)

    def test_fractional_acids_and_anions_remain_unchecked(self):
        for formula in ('C21H28N2·1.5HCl', 'C4H12N·0.5Cl'):
            self.assertEqual(parse_ea_formula(formula).status, 'unsupported')
            result, tracker = self.evaluate(f'Anal. Calcd for {formula}: C, 50.00. Found: C, 50.00.')
            self.assertFalse(result['formula_sources']['ea']['usable'])
            self.assertTrue(any('not checked by CheckSI' in item for item in result['warnings']))
            self.assertEqual(tracker.get_count('formula_invalid_format'), 0)

    def test_ambiguous_isotope_removal_and_rare_isotope_basis_stay_pending(self):
        for ion, formula, charge, reason in (('[M+Cl]-', 'C2H5[35Cl][37Cl]O', -1, 'which natural isotope'),
                ('[M+K]+', 'C2H6[41K]O', 1, 'isotope')):
            result, tracker = self.evaluate(ion_line(ion, formula, charge), 'OCCCl' if charge < 0 else 'CCO')
            self.assertIsNone(result['formula_sources']['hrms_neutral']['formula'])
            self.assertIn(reason, result['formula_sources']['hrms_neutral']['reason'])
            self.assertEqual(tracker.get_count('hrms_ion_balance_invalid'), 0)
            self.assertEqual(tracker.get_count('formula_source_review_required'), 1)

    def test_parent_formula_and_ion_labels_are_readable(self):
        result, tracker = self.evaluate(ea_line('C3H8O·H2O', 'C3H10O2'), 'CCO')
        warning, = [item for item in result['warnings'] if 'name_formula_mismatch.' in item]
        self.assertIn('gives C3H8O (mass 60.0575)', warning)
        self.assertNotIn('78.0681', warning)
        result, tracker = self.evaluate(ion_line('[M]+', 'C4H12N') + ea_line('C4H12N2O3'))
        self.assertIn('HRMS parent ion', _formula_html(result))
        result, tracker = self.evaluate(ion_line('[M]+', 'C3H8O'), 'CCCO')
        self.assertEqual(tracker.get_count('formula_source_review_required'), 1)
        self.assertIn('HRMS parent ion:', ' '.join(result['warnings']))
        self.assertNotIn('hrms_neutral:', ' '.join(result['warnings']))

    def test_public_name_comparison_retains_isotopic_modification_notice(self):
        for smiles, formula in (('CC[37Cl]', 'C2H5Cl'), ('CC[81Br]', 'C2H5Br')):
            tracker = ErrorTracker()
            output, _, _ = compare_name_and_formula(make_config(), tracker, [], Chem.MolFromSmiles(smiles),
                                                    Formula(formula).isotope.mass, formula)
            self.assertEqual(tracker.get_count('formula_source_review_required'), 1)
            self.assertEqual(tracker.get_count('name_formula_mismatch'), 0)
            self.assertTrue(any('isotopic modification' in line for line in output))

    def test_folded_parent_serialization_is_stable_across_hash_seeds(self):
        script = ('import json; from checksi.analysis.formula_source_policy import parse_ea_formula, integer_formula; '
                  'print(json.dumps([integer_formula(parse_ea_formula(text).comparable_parent) '
                  'for text in ("C17H19.4NO3.2", "C17.25H19.5Cl0.5NO3")]))')
        outputs = [subprocess.check_output([sys.executable, '-c', script],
                   env={**os.environ, 'PYTHONHASHSEED': str(seed)}, text=True).strip() for seed in (1, 2, 3)]
        self.assertEqual(len(set(outputs)), 1, outputs)
        self.assertEqual(json.loads(outputs[0]), ['C17H19NO3', 'C17H19NO3'])

    def test_ea_bullet_separator_preserves_full_percentages_and_evidence(self):
        for separator in ('•', 'ꞏ'):
            formula = 'C3H8O' + separator + 'H2O'
            text = ion_line('[M+H]+', 'C3H9O') + ea_line(formula, 'C3H10O2')
            original = find_ea_formula_candidate(text)
            self.assertEqual(original.formula, formula)
            self.assertEqual(parse_ea_formula(formula).status, 'accepted')
            result, tracker = self.evaluate(text)
            ea = result['formula_sources']['ea']
            self.assertEqual(ea['composition']['counts'], {'C': 3, 'H': 10, 'O': 2})
            self.assertEqual(self.pair(result)['status'], 'agree')
            self.assertEqual(tracker.get_count('ea_formula_calcd_mismatch'), 0)
            self.assertEqual(result['unchecked_formulas'], [])
            self.assertIn(formula, str(ea.get('normalization')))

    def test_factory_diagnostics_keep_wrong_formula_critical_and_pending_info(self):
        controller = CheckSIController(ConfigManager.load_factory())
        cases = (
            (ion_line('[M+H]+', 'C21H29N2') + ea_line('C22H30N2O'), None,
             'formula_source_mismatch', ErrorSeverity.CRITICAL),
            (ion_line('[M]+', 'C4H12N') + ea_line('C4H10ClN'), None,
             'formula_source_mismatch', ErrorSeverity.CRITICAL),
            (ion_line('[M]+', 'C4H12N'), 'CN(C)N(C)C',
             'name_formula_mismatch', ErrorSeverity.CRITICAL),
            (ion_line('[M]+', 'C4H12N') + ea_line('C4H12N2O3'), None,
             'formula_source_review_required', ErrorSeverity.INFO),
            (ion_line('[M+H]+', 'C2H7[34S]'), 'CSC',
             'formula_source_review_required', ErrorSeverity.INFO),
            (re.sub(r'H, \d+\.\d+', 'H, 50.00', ea_line('C17H18D3NO4')), None,
             'ea_isotope_calcd_mismatch', ErrorSeverity.WARNING),
        )
        for text, smiles, code, severity in cases:
            with self.subTest(code=code, text=text):
                result = controller._analyze_description_diagnostics(NMR + text, smiles, ErrorTracker())
                finding, = [item for item in result['warnings'] if isinstance(item, Finding) and item.code.value == code]
                self.assertEqual(finding.severity, severity)


if __name__ == '__main__':
    unittest.main()
