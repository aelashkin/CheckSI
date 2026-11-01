"""Whole listed-component proofs must not depend on an inferred species cap."""

import unittest
from fractions import Fraction
from unittest.mock import patch

from checksi.analysis import formula_source_policy as policy
from tests import test_formula_review_r3_core as core


class FormulaComponentFeasibilityTests(unittest.TestCase):
    def test_four_kind_fold_stays_pending_while_explicit_parent_agrees(self):
        case = core.FormulaReviewR3CoreTests()
        for formula, status in (
                ('C26H40Cl3N3O2S', 'not_comparable'),
                ('C21H28N2·H2O·C2H3N·C2H6OS·CHCl3', 'agree')):
            with self.subTest(formula=formula):
                result, tracker = case.evaluate(core.ion_line('[M+H]+', 'C21H29N2') +
                                                core.ea_line(formula, 'C26H40Cl3N3O2S'))
                self.assertEqual(case.pair(result)['status'], status)
                self.assertEqual(tracker.get_count('formula_source_mismatch'), 0)
                self.assertEqual(tracker.get_count('formula_source_review_required'), int(status != 'agree'))

    def test_unforced_four_kind_and_large_coefficients_have_exact_witnesses(self):
        for formula in ('C9H16Cl2O2', 'C9H2000000014Cl2O1000000001'):
            counts = policy.parse_ea_formula(formula).full_counts
            self.assertFalse(policy.explain_formula_difference(counts, {}).recognized)
            self.assertFalse(policy.has_three_kind_component_difference(counts, {}))
            result = policy.listed_component_feasibility(counts, {})
            self.assertEqual(result.status, 'feasible')
            self.assertGreater(len(result.components), 3)
            self.assertLessEqual(result.work, 4096)
            self.assert_exact_witness(counts, result.components)

    def test_name_only_four_kind_fold_stays_visibly_pending(self):
        case = core.FormulaReviewR3CoreTests()
        result, tracker = case.evaluate(core.ea_line('C26H40Cl3N3O2S'),
                                        'Cc1ccc(NCC2CCC(CNc3ccc(C)cc3)C2)cc1')
        pair = case.pair(result, 'ea', 'name')
        self.assertEqual(pair['status'], 'not_comparable')
        self.assertTrue(pair['review_required'])
        self.assertEqual(tracker.get_count('formula_source_review_required'), 1)
        self.assertEqual(tracker.get_count('name_formula_mismatch'), 0)

    def assert_exact_witness(self, expected, components):
        rebuilt = {}
        for component in components:
            self.assertGreater(component.amount, 0)
            self.assertEqual(component.amount.denominator, 1)
            for key, count in policy.parse_ea_formula(component.formula).full_counts.items():
                rebuilt[key] = rebuilt.get(key, Fraction()) + component.amount * count
        self.assertEqual(rebuilt, expected)

    def test_all_listed_kinds_can_contribute_without_a_species_cap(self):
        declaration = ('HCl·HBr·HI·H2O·CH4O·C2H6O·C2H3N·CH2Cl2·CHCl3·'
                       'C4H8O2·C2H6OS·C4H10O·C3H6O·C4H8O·C7H8·C2HF3O2')
        counts = policy.parse_ea_formula(declaration).full_counts
        result = policy.listed_component_feasibility(counts, {})
        self.assertEqual(result.status, 'feasible')
        self.assert_exact_witness(counts, result.components)
        self.assertLessEqual(result.work, 4096)

    def test_wrong_subset_and_boc_deltas_are_proven_infeasible(self):
        case = core.FormulaReviewR3CoreTests()
        parent = policy.parse_ea_formula('C21H28N2').full_counts
        for formula in ('C22H30N2O', 'C26H36N2O2', 'C22H31ClN2'):
            with self.subTest(formula=formula):
                result = policy.listed_component_feasibility(policy.parse_ea_formula(formula).full_counts, parent)
                self.assertEqual(result.status, 'infeasible')
                description, tracker = case.evaluate(core.ion_line('[M+H]+', 'C21H29N2') + core.ea_line(formula))
                self.assertEqual(case.pair(description)['status'], 'disagree')
                self.assertIn('exact whole-unit constraints', case.pair(description)['reason'])
                self.assertEqual(tracker.get_count('formula_source_mismatch'), 1)
                self.assertEqual(tracker.get_count('formula_source_review_required'), 0)

    def test_exhausted_budget_is_explicit_pending_without_a_component_claim(self):
        counts = policy.parse_ea_formula('C1000000000H1000000000O1000000000').full_counts
        result = policy.listed_component_feasibility(counts, {}, work_limit=32)
        self.assertEqual((result.status, result.reason, result.work), ('unknown', 'work_limit', 32))
        self.assertEqual(result.components, ())
        with patch.object(policy, '_COMPONENT_SEARCH_WORK_LIMIT', 32):
            case = core.FormulaReviewR3CoreTests()
            description, tracker = case.evaluate(core.ion_line('[M+H]+', 'C21H29N2') +
                core.ea_line('C1000000021H1000000028N2O1000000000'))
        pair = case.pair(description)
        self.assertEqual(pair['status'], 'not_comparable')
        self.assertTrue(pair['review_required'])
        self.assertIn('work limit', pair['reason'])
        self.assertIn('No component relationship was certified', pair['reason'])
        self.assertNotIn('consistent with', pair['reason'])
        self.assertEqual(tracker.get_count('formula_source_mismatch'), 0)
        self.assertEqual(tracker.get_count('formula_source_review_required'), 1)


if __name__ == '__main__':
    unittest.main()
