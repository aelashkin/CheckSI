"""Supported scientific source forms retain their checks and evidence."""

import unittest

from rdkit import Chem

from checksi.analysis.cross_checks import structure_formula_state
from checksi.analysis.hrms_fields import find_ea_formula_candidate, find_hrms_formula_candidate
from checksi.controller import CheckSIController
from checksi.utils.error_handler import ErrorTracker
from tests.test_formula_reconciliation import NMR, HRMS, EA_PROPANOL
from tests.test_hrms import make_config


class ScientificEvidenceRepairTests(unittest.TestCase):
    def evaluate(self, text, smiles='CCCO', overrides=None):
        tracker = ErrorTracker()
        result = CheckSIController(make_config(overrides=overrides))._analyze_single_description(NMR + text, smiles, tracker)
        return result, tracker

    def test_supported_hrms_publication_phrasings_keep_formula(self):
        for text, formula in (
            ('HRMS C12H12NaO3 requires 235.0733', 'C12H12NaO3'),
            *((f'HRMS C10H13O2 {ion} 165.0910', 'C10H13O2') for ion in ('(MH+)', '[MH]+', '(MNa+)')),
            ('HRMS C10H13O2 is 165.0910', 'C10H13O2'),
            ('HRMS calcd for C10H13O2 = 165.0910', 'C10H13O2'),
            ("HRMS calc'd for C10H13O2 165.0910", 'C10H13O2'),
            ('HRMS Calculated mass for C10H13O2 165.0910', 'C10H13O2'),
            ('HRMS calcd 165.0910 for C10H13O2', 'C10H13O2'),
            ('HRMS m/z for C10H13O2 165.0910', 'C10H13O2'),
        ):
            with self.subTest(text=text):
                candidate = find_hrms_formula_candidate(text)
                self.assertEqual(candidate.formula, formula)
                self.assertEqual(text[candidate.start:candidate.end], formula)

    def test_charged_and_rsc_phrasings_keep_the_parent_checks(self):
        for text, formula in (
            ('HRMS (ESI) calcd for C3H9O+ = 61.0900, found 61.0300.', 'C3H9O'),
            ('HRMS (ESI) C4H9O+ requires 61.0900, found 61.0300.', 'C4H9O'),
            ('HRMS (ESI) calcd for C10H13O2+ is 165.0910, found 165.0912.', 'C10H13O2'),
            ('HRMS (EI) Found: M+, 270.1252. C17H18O3 requires M, 270.1256.', 'C17H18O3'),
            ('HRMS (EI) (Found: M+, 270.1252. C17H18O3 requires M, 270.1256).', 'C17H18O3'),
            ('HRMS (ESI) Found: MH+, 165.0912. C10H13O2 requires MH, 165.0910.', 'C10H13O2'),
            ('HRMS (ESI) m/z 165.0912 (C10H13O2 requires 165.0910).', 'C10H13O2'),
            ('HRMS (ESI) m/z [M+H]+ 165.0912 (C10H13O2+ requires 165.0910).', 'C10H13O2'),
        ):
            with self.subTest(text=text):
                candidate = find_hrms_formula_candidate(text)
                self.assertEqual(candidate.formula, formula)
                self.assertEqual(text[candidate.start:candidate.end], formula)
        # Parent (#24) counts: the RSC sentence form leaves its masses to the existing parser.
        for text, mismatch, missing in (('HRMS (ESI) calcd for C3H9O+ = 61.0900, found 61.0300.', 1, 0),
                                        ('HRMS (ESI) C3H9O+ requires 61.0900, found 61.0300.', 1, 0),
                                        ('HRMS (ESI) Found: MH+, 61.0300. C3H9O requires MH, 61.0900.', 0, 1)):
            with self.subTest(text=text):
                result, tracker = self.evaluate(text)
                self.assertEqual(result['formula_sources']['hrms']['formula'], 'C3H9O')
                self.assertEqual(tracker.get_count('formula_invalid_format'), 0)
                self.assertEqual(tracker.get_count('calcd_formula_mass_mismatch'), mismatch)
                self.assertEqual(tracker.get_count('hrms_calcd_mass_missing'), missing)
        for raw in ('C3H9O+ hydrate', 'C3H9O+ x H2O', 'C2H6O-d6'):
            with self.subTest(raw=raw):
                candidate = find_hrms_formula_candidate(f'HRMS (ESI) calcd for {raw} 61.0648, found 61.0648.')
                self.assertEqual(candidate.raw, raw)
                self.assertIsNone(candidate.formula)
        for prefix in ('0.5', '2', '1/2'):
            with self.subTest(prefix=prefix):
                self.assertEqual(find_hrms_formula_candidate(f'HRMS (ESI) {prefix} C3H9O+ 61.0648').raw, f'{prefix} C3H9O')

    def test_and_before_a_field_word_ends_the_formula_after_its_mass(self):
        for text in ('HRMS (ESI) calcd for C10H13O2 165.0910 and found 165.0912.',
                     'HRMS (ESI) calcd for C10H13O2+ 165.0910 and found 165.0912.'):
            with self.subTest(text=text):
                self.assertEqual(find_hrms_formula_candidate(text).formula, 'C10H13O2')
        # No inverse adduct is available, so the surviving name peer needs review.
        result, tracker = self.evaluate('HRMS (ESI) calcd for C3H9O+ 61.0648 and found 61.0700.')
        self.assertEqual(result['formula_sources']['hrms']['formula'], 'C3H9O')
        self.assertEqual({code for code, count in tracker.to_dict().items() if count},
                         {'hrms_calcd_mass_missing', 'formula_source_review_required'})
        self.assertEqual(find_hrms_formula_candidate('HRMS calcd for C3H8O 60.0570 and H2O').raw,
                         'C3H8O 60.0570 and H2O')

    def test_ea_metadata_is_a_boundary_but_grouped_composition_remains_refused(self):
        for metadata in ('(356.42)', '(MW 356.42)', '(M.W. 356.42)', '(%)'):
            candidate = find_ea_formula_candidate(f'Anal. Calcd for C20H24N2O4 {metadata}: C, 67.40; H, 6.79.')
            self.assertEqual(candidate.formula, 'C20H24N2O4')
        for raw in ('C2H6O(H2O)', 'C2H6O (H2O)', 'C2H6O (opaque, grouped)'):
            candidate = find_ea_formula_candidate(f'Anal. Calcd for {raw}: C, 52.14;')
            self.assertEqual(candidate.raw, raw)
            self.assertIsNone(candidate.formula)

    def test_supported_formula_tokens_still_run_mass_and_percentage_checks(self):
        from checksi.analysis.elemental import check_elemental_analysis
        from checksi.analysis.hrms import check_hrms
        for metadata in ('(60.1)', '(%)', '(MW 60.1)'):
            tracker = ErrorTracker()
            text = EA_PROPANOL.replace('C3H8O:', f'C3H8O {metadata}:').replace('H, 13.42;', 'H, 1.00;', 1)
            _, formula, mass = check_elemental_analysis(make_config(), tracker, text, [])
            self.assertEqual(formula, 'C3H8O')
            self.assertGreater(mass, 0)
            self.assertGreater(tracker.get_count('ea_formula_calcd_mismatch'), 0)
        for prefix in ("calc'd for", 'Calculated mass for', 'm/z for'):
            tracker = ErrorTracker()
            _, formula, mass = check_hrms(make_config(), tracker,
                f'HRMS {prefix} C3H9O+ [M+H]+ 61.0000, found 61.0300.', [])
            self.assertEqual(formula, 'C3H9O')
            self.assertGreater(mass, 0)
            self.assertEqual(tracker.get_count('calcd_formula_mass_mismatch'), 1)
            self.assertEqual(tracker.get_count('hrms_ppm_too_large'), 2)

    def test_bracket_heavy_refusal_retains_complete_evidence(self):
        raw = '[C1]' * 8000
        candidate = find_hrms_formula_candidate('HRMS calcd for ' + raw)
        self.assertEqual(candidate.raw, raw)
        self.assertIsNone(candidate.formula)

    def test_charge_separated_neutral_structures_keep_name_comparison(self):
        for smiles in ('O=[N+]([O-])c1ccccc1', '[N-]=[N+]=Nc1ccccc1', '[O-][n+]1ccccc1'):
            with self.subTest(smiles=smiles):
                self.assertEqual(structure_formula_state(Chem.MolFromSmiles(smiles))[0], 'neutral')
                _, tracker = self.evaluate(EA_PROPANOL, smiles)
                self.assertEqual(tracker.get_count('name_formula_mismatch'), 1)
                self.assertEqual(tracker.get_count('formula_source_review_required'), 0)

    def test_adjacent_mass_and_molecular_ion_keep_neutral_basis(self):
        for ion in ('[M+H]+61.0648', '(M+H)+61.0648'):
            result, tracker = self.evaluate(HRMS.replace('[M+H]+ 61.0648', ion) + EA_PROPANOL)
            self.assertEqual([p['status'] for p in result['formula_comparisons']], ['agree'] * 3)
            self.assertEqual(tracker.get_count('formula_source_review_required'), 0)
        result, tracker = self.evaluate('HRMS calcd for C3H8O•+ [M]•+ 60.0570, found 60.0570. ' + EA_PROPANOL)
        self.assertTrue(result['formula_sources']['hrms_neutral']['comparable'])
        self.assertEqual(tracker.get_count('formula_source_review_required'), 0)

    def test_missing_adduct_requires_review_but_refused_ea_does_not(self):
        for text, expected in ((HRMS.replace('[M+H]+', '') + EA_PROPANOL, 1),
                               (HRMS + EA_PROPANOL.replace('C3H8O:', 'C3H8O(H2O):'), 0)):
            _, tracker = self.evaluate(text)
            self.assertEqual(tracker.get_count('formula_source_review_required'), expected)

    def test_legacy_name_mismatch_is_not_double_counted(self):
        for severity in ('disabled', 'info', 'warning', 'critical'):
            _, tracker = self.evaluate(HRMS + EA_PROPANOL, 'CCO', {'name_formula_mismatch': severity})
            self.assertEqual(tracker.get_count('name_formula_mismatch'), int(severity != 'disabled'))
            self.assertEqual(tracker.get_count('formula_source_mismatch'), 0)
