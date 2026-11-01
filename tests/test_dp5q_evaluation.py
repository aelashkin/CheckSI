"""Offline development-campaign inputs never invent missing signal coverage."""
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('dp5q_evaluate',Path(__file__).parents[1]/'scripts/evaluate_dp5q.py')
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


class EvaluationInputTests(unittest.TestCase):
    def test_shift_labels_groups_and_noncarbon_omissions_are_preserved(self):
        text='12.345678(any),80.0(C2,C3),1e2(C4)\n\n0.0(any)\n\nC2,C3\nOMIT Cl22\n'
        result=evaluation.parse_manual_text(text)
        self.assertEqual(result['carbon_shifts'],[12.345678,80.,100.])
        self.assertEqual(result['carbon_assignments'],[[],['C2','C3'],['C4']])
        self.assertEqual(result['equivalence_groups'],[['C2','C3']])
        self.assertEqual(result['omitted_labels'],['Cl22'])

    def test_invalid_nonfinite_and_oversized_inputs_fail(self):
        for text in ('nan(any)','inf(any)','1(C1','1(any),garbage','1(any)\n\n0(any)\n\nOMIT what','1'*16385):
            with self.subTest(text=text[:20]),self.assertRaises(ValueError):
                evaluation.parse_manual_text(text)

    def test_emitted_carbon_only_format_keeps_real_four_decimal_rounding(self):
        spectrum=evaluation.parse_manual_text('12.345678(any),80.0(C2,C3)\n\nmalformed proton section\n\nC2,C3\nOMIT Cl22')
        emitted=evaluation.emitted_nmr(spectrum)
        self.assertEqual(emitted,'12.3457(any),80.0(C2,C3)\n\n0.0(any)\n\nC2,C3\nOMIT Cl22\n')
        self.assertEqual(evaluation.parse_manual_text(emitted)['carbon_shifts'],[12.3457,80.])

    def test_fixed_labels_use_explicit_groups_then_original_index_singletons(self):
        spectrum=evaluation.parse_manual_text('12(any),80(C2,C3),100(C4)\n\n0(any)\n\nC2,C3')
        request=evaluation.request_for_case({'case_id':'software/unit','identity':{'carbon_indices':[0,1,2,3],'isomeric_smiles':'CCCC'}},spectrum)
        self.assertEqual(request.carbon_groups,(('C2','C3'),))
        self.assertEqual(request.fixed_assignments,{0:1,2:2})

    def test_full_frozen_manifest_keeps_all_candidates_and_exclusions(self):
        manifest=json.loads((Path(__file__).parent/'fixtures/dp5q/manifest.json').read_text())
        self.assertEqual(manifest['candidate_count'],370)
        self.assertEqual(len(manifest['cases']),370)
        self.assertEqual(len({row['case_id'] for row in manifest['cases']}),370)
        self.assertEqual(len({row['family_id'] for row in manifest['cases']}),66)
        self.assertEqual(sum(row['source_label_correct'] for row in manifest['cases']),66)
        self.assertEqual({row['family_id'] for row in manifest['exclusions'] if 'family_id'in row},
                         {'stereo/AT3','stereo/TS3A','stereo/TS4','stereo/NL1A'})
        s1=next(row for row in manifest['cases'] if row['case_id']=='revision/S1a')
        self.assertEqual(len(s1['identity']['carbon_indices']),15)
        self.assertEqual(len(manifest['spectra'][s1['spectrum_id']]['carbon_shifts']),14)
        self.assertTrue(all(row['pairing_status']=='unscorable_different_spectra' for row in manifest['cases'] if row['family_id']=='revision/S13'))


if __name__ == '__main__':
    unittest.main()
