"""Fast controls for the opt-in document-derived DP5q acceptance regression."""

import importlib.util
import json
from pathlib import Path
import unittest

from checksi.dp5q.contracts import CheckRequest


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location('acceptance', ROOT / 'scripts/check_dp5q_acceptance.py')
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


class AcceptanceRegressionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((ROOT / 'tests/fixtures/dp5q/acceptance-requests.json').read_text())
        self.outcomes = []
        for pair in self.fixture['part_a_pairs']:
            self.outcomes.extend([
                {'case_id': pair['original'], 'status': 'assessed', 'score_percent': 10.},
                {'case_id': pair['revised'], 'status': 'assessed', 'score_percent': 30.},
            ])
        self.outcomes.extend({'case_id': case, 'status': 'assessed', 'score_percent': score}
                             for case, score in zip(self.fixture['part_b_order'], [40., 30., 20., 1.]))

    def test_captured_requests_preserve_all_source_records_without_name_resolution(self):
        self.assertEqual(self.fixture['source']['sha256'],
                         '5c6d8828be4c198ad358a02fdfb1bee44c203fedeefa9df7209b3378c38da242')
        self.assertEqual(len(self.fixture['cases']), 32)
        self.assertEqual(len(self.fixture['part_a_pairs']), 14)
        families = ['3.2', '3.3', '3.4', '3.5', '3.7', '3.8', '3.9', '3.10',
                    '3.11', '3.17', '3.20', '3.22', '3.23', '3.24']
        self.assertEqual(self.fixture['part_a_pairs'],
                         [{'original': case, 'revised': case + ', revised'} for case in families])
        self.assertEqual(self.fixture['part_b_order'], ['1.1', '1.3', '1.4', '1.2'])
        self.assertEqual(self.fixture['score_threshold_percent'], 20.)
        self.assertEqual([case['case_id'] for case in self.fixture['cases']],
                         [case for family in families for case in (family, family + ', revised')]
                         + ['1.1', '1.2', '1.3', '1.4'])
        for index, case in enumerate(self.fixture['cases'], 1):
            request = CheckRequest.model_validate(case['request'])
            self.assertEqual(request.compound_id, str(index))
            self.assertEqual(case['source_paragraph_1based'], 2 * index)
            self.assertTrue(case['source_name'])
            self.assertIn('(' + case['case_id'] + ')', case['docx_name'])
        # These actual names leave stereo unspecified; historical SDFs differ.
        cases = {case['case_id']: case for case in self.fixture['cases']}
        self.assertEqual(cases['3.9, revised']['request']['smiles'],
                         'C#CC=CCC1OC(C2CC(Br)C(CC)O2)CC1Br')
        self.assertEqual(cases['3.10']['request']['smiles'],
                         'CC1=CCCC23C(=O)OC4C2C(C3C1=O)C4(C)C')

    def test_all_pairs_and_part_b_are_required_for_acceptance(self):
        report = acceptance.evaluate_criteria(self.fixture, self.outcomes)
        self.assertTrue(report['passed'], report['failures'])
        self.assertEqual(report['assessed_count'], 32)
        self.assertEqual(len(report['pairs']), 14)
        for pair in self.fixture['part_a_pairs']:
            with self.subTest(pair=pair['original']):
                outcomes = [dict(row) for row in self.outcomes]
                next(row for row in outcomes if row['case_id'] == pair['revised'])['score_percent'] = 10.
                report = acceptance.evaluate_criteria(self.fixture, outcomes)
                self.assertFalse(report['passed'])
                self.assertIn(pair['revised'], ' '.join(report['failures']))

    def test_missing_duplicate_unscored_and_nonfinite_results_cannot_pass(self):
        bad_rows = [None, {'status': 'not_applicable', 'reason': 'embedding_failed', 'score_percent': None},
                    {'status': 'failed', 'reason': 'protocol_error', 'score_percent': None},
                    {'status': 'assessed', 'score_percent': float('nan')},
                    {'status': 'assessed', 'score_percent': 101.}]
        for bad in bad_rows:
            with self.subTest(bad=bad):
                outcomes = [dict(row) for row in self.outcomes]
                original = outcomes.pop(0)
                if bad is not None:
                    outcomes.insert(0, {**original, **bad})
                report = acceptance.evaluate_criteria(self.fixture, outcomes)
                self.assertFalse(report['passed'])
                self.assertIn(original['case_id'], ' '.join(report['failures']))
        report = acceptance.evaluate_criteria(self.fixture, self.outcomes + [self.outcomes[0]])
        self.assertFalse(report['passed'])
        self.assertIn('duplicate', ' '.join(report['failures']))

    def test_every_part_b_ordering_and_wrong_spectrum_threshold_are_enforced(self):
        for left, right in zip(self.fixture['part_b_order'], self.fixture['part_b_order'][1:]):
            with self.subTest(left=left, right=right):
                outcomes = [dict(row) for row in self.outcomes]
                score = next(row['score_percent'] for row in outcomes if row['case_id'] == right)
                next(row for row in outcomes if row['case_id'] == left)['score_percent'] = score
                report = acceptance.evaluate_criteria(self.fixture, outcomes)
                self.assertFalse(report['passed'])
                self.assertIn('Part B', ' '.join(report['failures']))
        outcomes = [dict(row) for row in self.outcomes]
        next(row for row in outcomes if row['case_id'] == '1.2')['score_percent'] = 20.
        report = acceptance.evaluate_criteria(self.fixture, outcomes)
        self.assertFalse(report['passed'])
        self.assertIn('below 20', ' '.join(report['failures']))


if __name__ == '__main__':
    unittest.main()
