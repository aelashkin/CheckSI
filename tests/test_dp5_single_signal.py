"""One misplaced 13C signal in a description that passes the DP5q score."""

import unittest

from rdkit import Chem

from checksi.dp5q.single_signal import (GAIN_MIN_PPM, SingleSignal, carbon_environment, misplaced_signals,
                                           single_signal)

CALCULATED = [14.1, 22.9, 31.5, 60.8, 128.3, 129.6, 130.2, 132.9, 166.4]
REPORTED = [14.3, 22.7, 31.9, 60.9, 128.4, 129.5, 130.5, 132.8, 166.6]


class SingleSignalTests(unittest.TestCase):
    def test_correct_description_has_nothing_to_gain(self):
        item = single_signal(CALCULATED, REPORTED)
        self.assertLess(item.gain, 1.0)
        self.assertEqual(misplaced_signals({1: item}), {})

    def test_shifted_signal_is_found_although_the_ordered_pairing_spreads_it(self):
        # 128.4 typed as 148.4: in shift order every pair between 128 and 148 moves by one
        # place and no single pair is more than 16 ppm off.
        reported = [148.4 if value == 128.4 else value for value in REPORTED]
        pairs = [abs(c - e) for c, e in zip(sorted(CALCULATED), sorted(reported))]
        self.assertLess(max(pairs), 16.0)
        item = single_signal(CALCULATED, reported)
        self.assertEqual(item.experimental, 148.4)
        self.assertGreater(item.gain, GAIN_MIN_PPM)
        self.assertLess(item.rest_rms, 1.0)
        self.assertEqual(list(misplaced_signals({1: item})), [1])

    def test_description_that_is_wrong_everywhere_is_left_to_the_score(self):
        reported = [value + shift for value, shift in zip(REPORTED, (9, -8, 12, -11, 10, -9, 13, -12, 8))]
        self.assertEqual(misplaced_signals({1: single_signal(CALCULATED, reported)}), {})

    def test_lists_of_different_length_or_too_short_are_skipped(self):
        self.assertIsNone(single_signal(CALCULATED, REPORTED[:-1]))
        self.assertIsNone(single_signal([10.0, 20.0, 30.0], [10.0, 20.0, 30.0]))

    def test_environment_of_the_unmatched_carbon_is_returned(self):
        names = [f'c{index}' for index in range(len(CALCULATED))]
        reported = [148.4 if value == 128.4 else value for value in REPORTED]
        self.assertTrue(single_signal(CALCULATED, reported, names).environment.startswith('c'))


class SeriesTests(unittest.TestCase):
    def item(self, experimental, calculated, environment, gain=9.0):
        return SingleSignal(experimental, calculated, gain, 2.0, 20, environment)

    def test_same_carbon_off_by_the_same_amount_in_an_analogue_is_a_model_error(self):
        series = {1: self.item(105.8, 115.3, 'C(N)=C(F)C'), 2: self.item(105.2, 114.2, 'C(N)=C(F)C')}
        self.assertEqual(misplaced_signals(series), {})

    def test_single_compound_or_another_carbon_or_another_deviation_stays(self):
        self.assertEqual(list(misplaced_signals({1: self.item(105.8, 115.3, 'C(N)=C(F)C')})), [1])
        other_carbon = {1: self.item(105.8, 115.3, 'C(N)=C(F)C'), 2: self.item(105.2, 114.2, 'c(c)c')}
        self.assertEqual(sorted(misplaced_signals(other_carbon)), [1, 2])
        other_size = {1: self.item(105.8, 115.3, 'C(N)=C(F)C'), 2: self.item(128.0, 114.2, 'C(N)=C(F)C')}
        self.assertEqual(sorted(misplaced_signals(other_size)), [1, 2])

    def test_analogue_below_the_reporting_limit_still_marks_a_series(self):
        series = {1: self.item(105.8, 115.3, 'C(N)=C(F)C'), 2: self.item(106.9, 114.2, 'C(N)=C(F)C', gain=6.0)}
        self.assertEqual(misplaced_signals(series), {})

    def test_environment_is_the_same_in_analogues_and_differs_between_carbons(self):
        first, second = Chem.MolFromSmiles('CCOC(=O)c1ccc(C)cc1'), Chem.MolFromSmiles('CCOC(=O)c1ccc(Cl)cc1')
        self.assertEqual(carbon_environment(first, 3), carbon_environment(second, 3))      # the ester carbon
        self.assertNotEqual(carbon_environment(first, 3), carbon_environment(first, 0))


if __name__ == '__main__':
    unittest.main()
