"""Series in which DP5q misses the same carbon alike."""

import unittest

from checksi.dp5q.series import find_series


class SeriesTests(unittest.TestCase):
    def test_three_compounds_with_the_same_carbon_off_alike_form_a_series(self):
        deviations = {1: [('C(N)=C(F)C', 9.8), ('c(c)c', 1.0)], 2: [('C(N)=C(F)C', 9.4)], 3: [('C(N)=C(F)C', 10.9)],
                      4: [('C(N)=C(F)C', 0.4)]}
        (series,) = find_series(deviations)
        self.assertEqual((series.environment, series.members), ('C(N)=C(F)C', (1, 2, 3)))
        self.assertAlmostEqual(series.mean_error, 10.03, places=2)

    def test_two_compounds_or_small_or_scattered_errors_are_not_a_series(self):
        self.assertEqual(find_series({1: [('C', 9.0)], 2: [('C', 9.5)]}), [])
        self.assertEqual(find_series({1: [('C', 4.0)], 2: [('C', 4.5)], 3: [('C', 4.2)]}), [])
        self.assertEqual(find_series({1: [('C', 6.0)], 2: [('C', -6.0)], 3: [('C', 12.0)]}), [])

    def test_the_largest_consistent_group_is_taken(self):
        deviations = {1: [('C', 6.0)], 2: [('C', 6.5)], 3: [('C', 7.0)], 4: [('C', 14.0)]}
        self.assertEqual(find_series(deviations)[0].members, (1, 2, 3))


class TerminalCarbonTests(unittest.TestCase):
    def test_methyl_type_carbons_are_terminal(self):
        from rdkit import Chem
        from checksi.dp5_enrichment import _is_terminal
        from checksi.dp5q.single_signal import carbon_environment
        mol = Chem.MolFromSmiles('COc1ccc(C(C)(C)C)cc1')
        terminal = [atom.GetIdx() for atom in mol.GetAtoms()
                    if atom.GetSymbol() == 'C' and _is_terminal(carbon_environment(mol, atom.GetIdx()))]
        self.assertEqual(terminal, [0, 7, 8, 9])       # OCH3 and the three methyls of tert-butyl


if __name__ == '__main__':
    unittest.main()
