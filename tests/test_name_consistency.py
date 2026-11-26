"""A note where independent checks disagree with the named structure together."""

import unittest

from checksi.config import ConfigManager
from checksi.name_consistency import disagreeing_checks, enrich_analysis_with_name_notes

H1 = '❗ h1_shift_structure_mismatch. 1H NMR shifts deviate from CASCADE predictions ...'
H1B = '❗ h1_protons_unaccounted. 2 C-H proton(s) of the structure ...'
C13 = '❗ dp5_low_probability. DP5q compatibility score is 0.01% ...'
FORMULA = '⚠️ name_formula_mismatch. The formula from the name does not match ...'


def run(warnings):
    data = {'compounds': [{'id': 1, 'source_paragraph_1based': 1, 'all_warnings': list(warnings)}]}
    enrich_analysis_with_name_notes(ConfigManager.load_factory(), data)
    return data


class NameConsistencyTests(unittest.TestCase):
    def test_two_findings_of_one_model_count_once(self):
        self.assertEqual(disagreeing_checks([H1, H1B]), ['the 1H shifts'])
        self.assertEqual(run([H1, H1B])['error_stats'].get('name_data_mismatch', 0), 0)

    def test_two_independent_comparisons_give_one_note(self):
        data = run([H1, C13, 'hnmr_j_partner_mismatch. ...'])
        self.assertEqual(data['error_stats']['name_data_mismatch'], 1)
        note = data['compounds'][0]['all_warnings'][-1]
        self.assertIn('The 1H shifts and the 13C shifts all disagree', note)

    def test_three_comparisons_are_listed(self):
        note = run([FORMULA, H1B, C13])['compounds'][0]['all_warnings'][-1]
        self.assertIn('The HRMS or EA formula, the 1H shifts and the 13C shifts', note)

    def test_neutral_formula_after_an_ion_label_is_not_a_name_problem(self):
        def source(counts):
            return {'composition': {'counts': counts, 'usable': True}}
        written = {'C': 17, 'H': 18, 'N': 1, 'O': 3, 'P': 1}
        for name_counts, expected in ((dict(written), 0), ({**written, 'H': 20}, 1)):
            data = {'compounds': [{'id': 1, 'source_paragraph_1based': 1, 'all_warnings': [FORMULA, C13],
                                   'formula_sources': {'hrms': source(written), 'name': source(name_counts)}}]}
            enrich_analysis_with_name_notes(ConfigManager.load_factory(), data)
            self.assertEqual(data['error_stats'].get('name_data_mismatch', 0), expected)

    def test_single_or_unrelated_findings_give_nothing(self):
        for warnings in ([C13], [FORMULA, '⚠️ hrms_ppm_too_large. ...'], []):
            self.assertEqual(run(warnings)['error_stats'].get('name_data_mismatch', 0), 0)


if __name__ == '__main__':
    unittest.main()
