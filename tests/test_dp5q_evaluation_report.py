"""Refusal-aware evaluation counts and rank gates preserve every denominator."""
import importlib.util
from hashlib import sha256
import json
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('dp5q_report',Path(__file__).parents[1]/'scripts/dp5q-reference/report.py')
report=importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def row(case,correct,score=None,status='assessed'):
    result={'status':status,'score_raw':score,'log_score':None,'reason':None if status=='assessed' else 'refused'}
    return {'case_id':case,'family_id':'revision/unit','source_label_correct':correct,'arms':{'B':result,'C':dict(result)}}


class EvaluationReportTests(unittest.TestCase):
    @staticmethod
    def _method_rows(c_policy='bounded-ensemble-v3'):
        """Record the geometry method actually used, including refused scores."""
        generated = row('generated', True, None, 'not_applicable')
        generated['shared_generation_audit'] = {'generated_count': 30}
        generated['arms']['B']['provenance'] = {'geometry_policy': 'converged-single-v2'}
        generated['arms']['C']['provenance'] = {'geometry_policy': c_policy}
        return [generated]

    def test_historical_ensemble_method_is_stale_without_relabeling_its_results(self):
        methods = report.geometry_method_evidence(self._method_rows('bounded-ensemble-v1'))
        self.assertEqual(methods['B']['status'], 'ESTABLISHED')
        self.assertEqual(methods['C']['status'], 'STALE')
        self.assertEqual(methods['C']['recorded_policies'], ['bounded-ensemble-v1'])
        self.assertEqual(methods['C']['current_policy'], 'bounded-ensemble-v3')

    def test_missing_mixed_unknown_and_unrun_method_evidence_is_not_established(self):
        missing = self._method_rows()
        del missing[0]['arms']['C']['provenance']
        mixed = self._method_rows() + self._method_rows('bounded-ensemble-v1')
        for rows, status in ((missing, 'NOT_ESTABLISHED'), (mixed, 'MIXED'),
                             (self._method_rows('unknown-method'), 'NOT_ESTABLISHED'),
                             ([row('no-geometry', True, None, 'not_applicable')], 'NOT_RUN')):
            with self.subTest(status=status):
                self.assertEqual(report.geometry_method_evidence(rows)['C']['status'], status)

    def test_selection_requires_recorded_current_methods_and_keeps_fallback_explicit(self):
        metrics = {'0.1': {arm: {'incorrect': {'compatible': 0, 'assessed': 1},
                               'correct': {'flagged': 0, 'assessed': 1}} for arm in ('B', 'C')}}
        ranks = {arm: {} for arm in ('B', 'C')}
        timing = {'B_warm_calculation_seconds': 1, 'C_warm_calculation_seconds': 1}
        historical = report.geometry_method_evidence(self._method_rows('bounded-ensemble-v1'))
        selected = report.gate_report(metrics, ranks, timing, historical)
        self.assertEqual(selected['selected_policy'], 'converged-single-v2')
        self.assertFalse(selected['gates']['current_geometry_methods_established'])
        unknown = self._method_rows()
        del unknown[0]['arms']['B']['provenance']
        selected = report.gate_report(metrics, ranks, timing, report.geometry_method_evidence(unknown))
        self.assertEqual(selected['selected_policy'], 'NOT_ESTABLISHED')

    def test_refusal_is_not_acceptance_and_zero_is_a_valid_flagged_score(self):
        rows=[row('positive',True,.10),row('negative',False,0.),row('refused',False,None,'not_applicable')]
        metrics=report.class_metrics(rows,'B',.10)
        self.assertEqual(metrics['correct']['compatible'],1)
        self.assertEqual(metrics['incorrect']['submitted'],2)
        self.assertEqual(metrics['incorrect']['assessed'],1)
        self.assertEqual(metrics['incorrect']['unassessed'],1)
        self.assertEqual(metrics['incorrect']['compatible'],0)
        self.assertEqual(metrics['incorrect']['flagged'],1)
        self.assertEqual(metrics['incorrect']['coverage'],.5)

    def test_unadjudicated_jb13_pair_cannot_enter_class_metrics_or_ranks(self):
        conflicting = [row('JB13A_1',True,.9),row('JB13B_1',True,.1)]
        for candidate, family in zip(conflicting,('stereo/JB13A','stereo/JB13B')):
            candidate['family_id'] = family
        rows = [row('eligible',False,.5)] + conflicting
        metrics = report.class_metrics(rows,'B',.1)
        self.assertEqual(metrics['correct']['submitted'],0)
        self.assertEqual(metrics['incorrect']['submitted'],1)
        manifest = {'cases':[{'family_id':candidate['family_id']} for candidate in rows]}
        ranks = report.family_ranks(conflicting,manifest,'B')
        self.assertEqual(ranks['stereo/JB13A']['status'],'unadjudicated_labels')
        self.assertEqual(ranks['stereo/JB13B']['status'],'unadjudicated_labels')

    def test_every_prior_geometry_version_is_stale(self):
        generated = self._method_rows('bounded-ensemble-v2')
        generated[0]['arms']['B']['provenance']['geometry_policy'] = 'converged-single-v1'
        methods = report.geometry_method_evidence(generated)
        self.assertEqual({arm:value['status'] for arm,value in methods.items()}, {'B':'STALE','C':'STALE'})
        metrics = {'0.1': {arm: {'incorrect': {'compatible': 0, 'assessed': 1},
                               'correct': {'flagged': 0, 'assessed': 1}} for arm in ('B', 'C')}}
        selected = report.gate_report(metrics, {'B':{}, 'C':{}},
                                     {'B_warm_calculation_seconds':1., 'C_warm_calculation_seconds':1.}, methods)
        self.assertEqual(selected['selected_policy'],'NOT_ESTABLISHED')

    def test_nonfinite_or_missing_assessed_score_is_an_error(self):
        for score in (None,float('nan'),float('inf'),True,1.1):
            with self.subTest(score=score),self.assertRaises(ValueError):
                report.classification({'status':'assessed','score_raw':score},.10)

    def test_ties_keep_a_rank_interval_and_incomplete_families_are_not_ranked(self):
        rows=[row('correct',True,.5),row('wrong',False,.5)]
        manifest={'cases':[{'family_id':'revision/unit'},{'family_id':'revision/unit'}]}
        ranked=report.family_ranks(rows,manifest,'B')['revision/unit']
        self.assertEqual((ranked['rank_best'],ranked['rank_worst']),(1,2))
        self.assertFalse(ranked['unique_correct_first'])
        self.assertEqual(report.family_ranks(rows[:1],manifest,'B')['revision/unit']['status'],'incomplete_selection')
        rows[1]['arms']['B'].update(status='not_applicable',score_raw=None)
        self.assertEqual(report.family_ranks(rows,manifest,'B')['revision/unit']['status'],'unassessed')

    def test_log_scores_order_display_underflow_without_a_false_zero_tie(self):
        rows=[row('correct',True,0.),row('wrong',False,0.)]
        rows[0]['arms']['B']['log_score']=-1000.
        rows[1]['arms']['B']['log_score']=-2000.
        manifest={'cases':[{'family_id':'revision/unit'},{'family_id':'revision/unit'}]}
        self.assertTrue(report.family_ranks(rows,manifest,'B')['revision/unit']['unique_correct_first'])

    def test_invalid_log_scores_cannot_silently_change_family_rank(self):
        manifest={'cases':[{'family_id':'revision/unit'}]}
        for value in (True,'-1',float('nan'),float('inf'),.1):
            rows=[row('correct',True,.5)]
            rows[0]['arms']['B']['log_score']=value
            with self.subTest(value=value),self.assertRaises(ValueError):
                report.family_ranks(rows,manifest,'B')

    def test_reference_tampering_cannot_be_reported_or_pass_a_final_seal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            owned=root/'owned'
            reference=root/'reference'
            for folder in (owned/'case-000',reference/'case-000'):
                folder.mkdir(parents=True)
            manifest=root/'manifest.json'
            manifest.write_text(json.dumps({'source_revision':'frozen'}))
            bundle={'manifest':{'source_revision':'frozen'},'execution':{'case_paths':{'unit':'case-000'}},
                'case_ids':['unit'],'comparisons':[{'case_id':'unit','arm':'F'}],'seal_sha256':'fixed'}
            (owned/'case-000/result.json').write_text(json.dumps({'case_id':'unit'}))
            (reference/'case-000/reference.json').write_text(json.dumps({'case_id':'unit','arms':{}}))
            array=reference/'case-000/F-legacy.npz'
            array.write_bytes(b'hashed-array-placeholder')
            preflight=reference/'owned-preflight.json'
            preflight.write_text(json.dumps(bundle))
            def metadata(path):return {'bytes':path.stat().st_size,'sha256':sha256(path.read_bytes()).hexdigest()}
            receipt={'status':'PASS actual TensorFlow campaign parity','source_unchanged':True,'source_revision':'frozen',
                'manifest_sha256':sha256(manifest.read_bytes()).hexdigest(),'owned_evidence_seal_sha256':'fixed',
                'resources':{'user_cpu_seconds':.1,'system_cpu_seconds':.1},'case_directories':['case-000'],'case_count':1,
                'reference_artifacts':{name:metadata(reference/name) for name in ('case-000/reference.json','case-000/F-legacy.npz')},
                'reference_json_count':1,'legacy_array_count':1,'artifact_count':2,'comparison_count':1,
                'owned_preflight_file':dict(path='owned-preflight.json',**metadata(preflight))}
            (reference/'receipt.json').write_text(json.dumps(receipt))
            guard=SimpleNamespace(preflight_owned=lambda *args:bundle,completed_comparisons=lambda *args:1,validate_seal=lambda *args:None)
            with patch.object(report,'_campaign_guard',return_value=guard):
                loaded=report.campaign_inputs(manifest,owned,reference)
                array.write_bytes(b'tampered-array')
                with self.assertRaises(ValueError):
                    report.validate_input_seals(bundle,loaded[-1])
                with self.assertRaises(ValueError):
                    report.campaign_inputs(manifest,owned,reference)

    def test_numerical_controls_require_every_reference_comparison(self):
        plan={'comparisons':[{'case_id':'unit','arm':'F'},{'case_id':'unit','arm':'A'}]}
        complete={'status':'COMPLETE diagnostic same-input fitter and assignment controls','case_arm_count':2,
            'results':[{'case_id':'unit','arm':'F'},{'case_id':'unit','arm':'A'}]}
        self.assertEqual(len(report.validate_fit_results(complete,plan)),2)
        for results in ([],complete['results'][:1],[complete['results'][0]]*2):
            with self.subTest(results=results),self.assertRaises(ValueError):
                report.validate_fit_results(dict(complete,results=results),plan)


if __name__=='__main__':
    unittest.main()
