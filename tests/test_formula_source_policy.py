"""Offline policy cases adjudicated by Pavel (2026-10-26, answers Q6–9)."""

from fractions import Fraction
import unittest

from molmass import Formula

from checksi.analysis.formula_source_policy import (
    ComponentAmount,
    ea_molar_mass,
    ea_percentages,
    explain_formula_difference,
    match_counterion,
    natural_isotope_eligibility,
    parse_ea_formula,
)


def counts(formula):
    return parse_ea_formula(formula).full_counts


class EAFormulaPolicyTests(unittest.TestCase):
    def test_pavel_folded_partial_hydrates_keep_exact_counts(self):
        for formula, parent, amount in (
            ('C17H19.4NO3.2', 'C17H19NO3', Fraction(1, 5)),
            ('C10H13O2.5', 'C10H12O2', Fraction(1, 2)),
        ):
            with self.subTest(formula=formula):
                result = parse_ea_formula(formula)
                self.assertEqual(result.status, 'accepted')
                self.assertEqual(result.raw_formula, formula)
                self.assertIn((counts(parent), (ComponentAmount('H2O', amount),)),
                              [(option.parent_counts, option.components)
                               for option in result.parent_options])
                expected = Formula(parent).mass + float(amount) * Formula('H2O').mass
                self.assertAlmostEqual(ea_molar_mass(result.full_counts), expected, places=10)
                self.assertAlmostEqual(sum(ea_percentages(result.full_counts).values()), 100.0)
        self.assertEqual(counts('C17H19.4NO3.2')['H'], Fraction(97, 5))
        self.assertEqual(counts('C10H13O2.5')['O'], Fraction(5, 2))

    def test_folded_partial_common_solvents_are_recognized_exactly(self):
        for folded, solvent in (
            ('C20H41N2O4.5', 'H2O'),
            ('C20.5H42N2O4.5', 'MeOH'),
            ('C21H43N2O4.5', 'EtOH'),
            ('C21H41.5N2.5O4', 'MeCN'),
            ('C20.5H41ClN2O4', 'CH2Cl2'),
            ('C20.5H40.5Cl1.5N2O4', 'CHCl3'),
            ('C21H43N2O4.5S0.5', 'DMSO'),
            ('C22H45N2O4.5', 'Et2O'),
            ('C21.5H43N2O4.5', 'acetone'),
            ('C22H44N2O4.5', 'THF'),
            ('C23.5H44N2O4', 'toluene'),
        ):
            with self.subTest(solvent=solvent):
                result = parse_ea_formula(folded)
                self.assertEqual(result.status, 'accepted')
                self.assertIn((counts('C20H40N2O4'), (ComponentAmount(solvent, Fraction(1, 2)),)),
                              [(option.parent_counts, option.components) for option in result.parent_options])
        # A half equivalent of EtOAc folds entirely into integer atom counts.
        self.assertEqual(parse_ea_formula('C20H40N2O4·0.5C4H8O2').full_counts, counts('C22H44N2O5'))

    def test_fractional_carbon_typo_has_no_common_solvent_explanation(self):
        result = parse_ea_formula('C2.1H26N2')
        self.assertEqual(result.status, 'typo')
        self.assertEqual(result.full_counts['C'], Fraction(21, 10))
        self.assertEqual(result.parent_options, ())
        self.assertIsNone(result.comparable_parent)

    def test_percentages_use_bulk_element_masses_for_integer_controls(self):
        for formula in ('C2H6O', 'C21H30Cl2N2', 'C6H5[13C][2H]O'):
            with self.subTest(formula=formula):
                expected = {}
                for key, entry in Formula(formula).composition().items():
                    symbol = key.lstrip('0123456789')
                    expected[symbol] = expected.get(symbol, 0) + entry.fraction * 100
                actual = ea_percentages(counts(formula))
                self.assertEqual(actual.keys(), expected.keys())
                for symbol, percentage in expected.items():
                    self.assertAlmostEqual(actual[symbol], percentage, places=12)

    def test_partial_solvent_percentages_include_entire_fractional_declaration(self):
        result = parse_ea_formula('C10H13O2.5')
        # Independent locked-data weighted mass expression, without string expansion.
        mass_c, mass_h, mass_o = (Formula(symbol).mass for symbol in ('C', 'H', 'O'))
        total = 10 * mass_c + 13 * mass_h + 2.5 * mass_o
        self.assertAlmostEqual(ea_percentages(result.full_counts)['C'], 100 * 10 * mass_c / total)
        self.assertAlmostEqual(ea_percentages(result.full_counts)['H'], 100 * 13 * mass_h / total)
        self.assertAlmostEqual(ea_percentages(result.full_counts)['O'], 100 * 2.5 * mass_o / total)

    def test_dot_salts_and_hydrates_include_all_atoms(self):
        for declared, folded in (
            ('C21H28N2·2HCl', 'C21H30Cl2N2'),
            ('C21H28N2·H2O', 'C21H30N2O'),
            ('C17H19NO3·0.2H2O', 'C17H19.4NO3.2'),
            ('C2H6O.H2O', 'C2H8O2'),
        ):
            with self.subTest(declared=declared):
                result = parse_ea_formula(declared)
                self.assertEqual(result.status, 'accepted')
                self.assertEqual(result.full_counts, counts(folded))
                self.assertTrue(result.explicit_components)
                self.assertEqual(ea_percentages(result.full_counts), ea_percentages(counts(folded)))
        self.assertEqual(parse_ea_formula('C21H28N2·2HCl').comparable_parent, counts('C21H28N2'))
        self.assertEqual(parse_ea_formula('C10H20N·Cl').status, 'accepted')

    def test_unsupported_dot_components_never_expose_partial_counts(self):
        result = parse_ea_formula('C2H6·NaCl·H2O')
        self.assertEqual(result.status, 'unsupported')
        self.assertEqual(result.full_counts, counts('C2H8ClNaO'))
        self.assertIsNone(result.comparable_parent)

    def test_ambiguous_fractional_decomposition_does_not_pick_a_parent(self):
        # A half methanol and a half ethanol both fit this folded composition.
        result = parse_ea_formula('C10.5H13O2.5')
        self.assertEqual(result.status, 'accepted')
        self.assertGreater(len(result.parent_options), 1)
        self.assertIsNone(result.comparable_parent)

    def test_parser_preserves_fixed_isotopes_and_bounds_hostile_inputs(self):
        self.assertEqual(counts('C2H5D')['2H'], 1)
        self.assertEqual(counts('C2H5[2H]')['2H'], 1)
        self.assertEqual(counts('C2H4(OH)2'), counts('C2H6O2'))
        for formula in ('C' + '9' * 1000, '(' * 1000 + 'C', 'C2Xx3', 'C0', 'C2H6O+', '2H2O'):
            with self.subTest(formula=formula):
                self.assertEqual(parse_ea_formula(formula).status, 'unsupported')


class FormulaDifferencePolicyTests(unittest.TestCase):
    def test_whole_acids_and_common_solvents_are_information_only_explanations(self):
        for extra in ('HCl', 'HBr', 'HI', 'H2O', 'C2HF3O2', 'CH4O', 'C2H6O',
                      'C2H3N', 'CH2Cl2', 'CHCl3', 'C4H8O2', 'C2H6OS', 'C4H10O',
                      'C3H6O', 'C4H8O', 'C7H8'):
            with self.subTest(extra=extra):
                result = explain_formula_difference(counts('C21H28N2·' + extra), counts('C21H28N2'))
                self.assertTrue(result.recognized)
                self.assertTrue(result.options)
        result = explain_formula_difference(counts('C21H30Cl2N2'), counts('C21H28N2'))
        self.assertEqual(result.options, ((ComponentAmount('HCl', Fraction(2)),),))

    def test_ambiguous_two_kind_difference_retains_both_explanations(self):
        result = explain_formula_difference(counts('C21H28N2·H2O·C4H8O'), counts('C21H28N2'))
        self.assertEqual(result.options, (
            (ComponentAmount('H2O', Fraction(1)), ComponentAmount('THF', Fraction(1))),
            (ComponentAmount('MeOH', Fraction(1)), ComponentAmount('acetone', Fraction(1))),
        ))
        self.assertFalse(result.explicit)
        self.assertIn('identity is not established', result.reason)

    def test_different_fixed_isotopes_do_not_become_solvent_or_counterion_matches(self):
        self.assertFalse(explain_formula_difference(counts('C2H4[18O]'), counts('C2H4O')).recognized)
        self.assertIsNone(match_counterion(counts('C10H20N[37Cl]'), counts('C10H20N'), 1))

    def test_large_stoichiometry_uses_direct_equations(self):
        result = explain_formula_difference(counts('C21H28N2·1000000000H2O'), counts('C21H28N2'))
        self.assertEqual(result.options, ((ComponentAmount('H2O', Fraction(10**9)),),))

    def test_wrong_ea_ch2_and_h2_controls_are_unexplained(self):
        for wrong in ('C22H30N2', 'C21H30N2', 'C20H28N2'):
            self.assertFalse(explain_formula_difference(counts(wrong), counts('C21H28N2')).recognized)

    def test_explicit_declaration_wins_over_minimal_kind_composition(self):
        result = parse_ea_formula('C21H28N2·C2H6O·H2O')
        preferred = explain_formula_difference(result.full_counts, counts('C21H28N2'),
                                              declared_components=result.explicit_components)
        self.assertTrue(preferred.explicit)
        self.assertEqual(preferred.options, (result.explicit_components,))
        folded = explain_formula_difference(result.full_counts, counts('C21H28N2'))
        self.assertEqual(folded.options, ((ComponentAmount('MeOH', Fraction(2)),),))

    def test_bounded_two_component_explanation_and_partial_explicit_water(self):
        result = explain_formula_difference(counts('C21H28N2·2HCl·H2O'), counts('C21H28N2'))
        self.assertIn((ComponentAmount('HCl', Fraction(2)), ComponentAmount('H2O', Fraction(1))), result.options)
        partial = parse_ea_formula('C17H19NO3·0.2H2O')
        self.assertFalse(explain_formula_difference(partial.full_counts, counts('C17H19NO3')).recognized)
        self.assertTrue(explain_formula_difference(partial.full_counts, counts('C17H19NO3'),
                                                  declared_components=partial.explicit_components).recognized)


class CounterionPolicyTests(unittest.TestCase):
    def test_all_pavel_anions_and_charge_balance(self):
        for name, formula in (('Cl', 'Cl'), ('Br', 'Br'), ('I', 'I'), ('BF4', 'BF4'),
                              ('PF6', 'PF6'), ('OTf', 'CF3O3S'), ('NTf2', 'C2F6NO4S2'),
                              ('ClO4', 'ClO4'), ('SbF6', 'SbF6'), ('BArF', 'C32H12BF24'),
                              ('OTs', 'C7H7O3S'), ('OAc', 'C2H3O2')):
            with self.subTest(name=name):
                matched = match_counterion(counts('C10H20N·' + formula), counts('C10H20N'), 1)
                self.assertEqual(matched.name, name)
                self.assertEqual(matched.amount, 1)
                self.assertIsNone(match_counterion(counts('C10H20N·' + formula), counts('C10H20N'), 2))
        self.assertEqual(match_counterion(counts('C10H20N2·2Cl'), counts('C10H20N2'), 2).amount, 2)
        self.assertIsNone(match_counterion(counts('C10H20N·Cl·Br'), counts('C10H20N'), 2))
        self.assertIsNone(match_counterion(counts('C10H20N·NO3'), counts('C10H20N'), 1))
        self.assertIsNone(match_counterion(counts('C10H20N·Cl'), counts('C10H20N'), 0))


class NaturalIsotopePolicyTests(unittest.TestCase):
    def test_pavel_most_abundant_and_at_least_ten_percent_examples(self):
        eligible = natural_isotope_eligibility()
        for label in ('11B', '19F', '28Si', '31P', '80Se', '120Sn', '127I', '23Na',
                      '39K', '56Fe', '63Cu', '106Pd', '195Pt', '37Cl', '81Br', '10B',
                      '65Cu', '109Ag', '66Zn', '118Sn'):
            element = label.lstrip('0123456789')
            self.assertIn(int(label[:-len(element)]), eligible[element], label)

    def test_labelling_isotopes_stay_distinct_and_uncovered_stay_unknown(self):
        eligible = natural_isotope_eligibility()
        for element, isotope in (('H', 2), ('C', 13), ('N', 15), ('O', 17), ('O', 18),
                                 ('S', 34), ('Pt', 198)):
            self.assertNotIn(isotope, eligible[element])
        self.assertNotIn(999, eligible['O'])
        self.assertNotIn('Ng', eligible)
        self.assertIn(1, eligible['H'])
        self.assertIn(12, eligible['C'])


if __name__ == '__main__':
    unittest.main()
