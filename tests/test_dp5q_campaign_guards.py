"""Small evidence/control fixtures never load models or run a scientific campaign."""

from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_script(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


evaluation = load_script('guard_evaluation', 'scripts/evaluate_dp5q.py')
reference = load_script('guard_reference', 'scripts/dp5q-reference/campaign.py')


class ReferencePreflightTests(unittest.TestCase):
    """Reconcile complete owned evidence before any TensorFlow/reference import."""

    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.directory)
        self.manifest_path = self.root / 'manifest.json'
        self.manifest_path.write_bytes((ROOT / 'tests/fixtures/dp5q/manifest.json').read_bytes())
        self.manifest = json.loads(self.manifest_path.read_text())
        self.owned = self.root / 'owned'
        self.owned.mkdir()
        self.ids = {case['case_id']: index for index, case in enumerate(self.manifest['cases'])}
        self.run = {
            'manifest_sha256': sha256(self.manifest_path.read_bytes()).hexdigest(),
            'source_revision': evaluation.SOURCE_REVISION, 'mode': 'pilot8',
            'case_ids': list(evaluation.PILOT_IDS), 'CPU_budget_seconds': 36000,
            'reference_CPU_reserve_seconds': 3600, 'output_budget_bytes': 512 * 1024 * 1024,
            'per_case_wall_limit_seconds': 600, 'code_hashes': {'fixture.py': '0' * 64},
            'git_commit': '0' * 40,
        }
        self.execution = {**self.run, 'status': 'PASS owned campaign execution',
            'completed': 8, 'returncode': 0, 'timed_out': False, 'child_CPU_seconds': 1.0,
            'case_paths': {name: f'case-{self.ids[name]:03d}' for name in evaluation.PILOT_IDS}}
        self.results = {}
        for number, case_id in enumerate(evaluation.PILOT_IDS):
            case = self.manifest['cases'][self.ids[case_id]]
            folder = self.owned / self.execution['case_paths'][case_id]
            folder.mkdir()
            (folder / 'emitted.nmr').write_text('10.0(any)\n\n0.0(any)\n')
            (folder / 'F.sdf').write_text('fixture geometry\n')
            arms = {arm: {'status': 'not_applicable', 'reason': 'strict_nmr_coverage_mismatch',
                          'score_raw': None, 'log_score': None} for arm in 'FABC'}
            if number == 0:
                for arm in 'FA':
                    content = f'{case_id}:{arm}:numeric fixture'.encode()
                    (folder / (arm + '-heads.npz')).write_bytes(content)
                    (folder / (arm + '.sdf')).write_text('fixture geometry\n')
                    arms[arm] = {'compound_id': case_id, 'status': 'assessed', 'reason': None,
                        'score_raw': 0.5, 'log_score': -0.693147, 'heads_sha256': sha256(content).hexdigest()}
            self.results[case_id] = {key: case[key] for key in ('case_id', 'family_id', 'spectrum_id', 'source_label_correct')}
            self.results[case_id]['arms'] = arms
        self.save()

    def save(self):
        for name, data in (('run.json', self.run), ('execution.json', self.execution)):
            (self.owned / name).write_text(json.dumps(data))
        for case_id, data in self.results.items():
            (self.owned / f'case-{self.ids[case_id]:03d}' / 'result.json').write_text(json.dumps(data))

    def preflight(self):
        return reference.preflight_owned(self.manifest_path, self.owned)

    def test_complete_pilot_records_comparisons_explicit_skips_and_seal(self):
        plan = self.preflight()
        self.assertEqual(plan['case_ids'], list(evaluation.PILOT_IDS))
        self.assertEqual(len(plan['comparisons']), 2)
        self.assertEqual(len(plan['skips']), 14)
        reference.validate_seal(plan)
        (self.owned / self.execution['case_paths'][evaluation.PILOT_IDS[0]] / 'emitted.nmr').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'changed|seal'):
            reference.validate_seal(plan)

    def test_incomplete_returncode_timeout_count_and_missing_cases_fail(self):
        for key, value in (('status', 'INCOMPLETE execution; not a scientific refusal'),
                           ('returncode', 7), ('timed_out', True), ('completed', 0),
                           ('mode', 'full'), ('case_ids', list(evaluation.PILOT_IDS[:-1]))):
            with self.subTest(key=key):
                saved = deepcopy(self.execution)
                self.execution[key] = value
                self.save()
                with self.assertRaises(ValueError):
                    self.preflight()
                self.execution = saved
        self.execution['case_paths'] = {}
        self.save()
        with self.assertRaises(ValueError):
            self.preflight()

    def test_path_mapping_and_each_result_identity_are_reconciled(self):
        first = evaluation.PILOT_IDS[0]
        self.execution['case_paths'][first] = 'case-999'
        self.save()
        with self.assertRaises(ValueError):
            self.preflight()
        self.execution['case_paths'][first] = f'case-{self.ids[first]:03d}'
        self.results[first]['case_id'] = 'other'
        self.save()
        with self.assertRaises(ValueError):
            self.preflight()

    def test_missing_tampered_or_undeclared_assessed_heads_fail(self):
        first = evaluation.PILOT_IDS[0]
        path = self.owned / self.execution['case_paths'][first] / 'F-heads.npz'
        original = path.read_bytes()
        path.unlink()
        with self.assertRaises(ValueError):
            self.preflight()
        path.write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            self.preflight()
        path.write_bytes(original)
        del self.results[first]['arms']['F']['heads_sha256']
        self.save()
        with self.assertRaises(ValueError):
            self.preflight()

    def test_every_declared_artifact_is_required_and_zero_comparisons_fail(self):
        first = evaluation.PILOT_IDS[0]
        self.results[first]['artifacts'] = {'B-geometry.npz': {'bytes': 2, 'sha256': 'a' * 64}}
        self.save()
        with self.assertRaises(ValueError):
            self.preflight()
        del self.results[first]['artifacts']
        for arm in 'FA':
            self.results[first]['arms'][arm] = {'status': 'not_applicable', 'reason': 'strict_nmr_coverage_mismatch',
                                               'score_raw': None, 'log_score': None}
            (self.owned / self.execution['case_paths'][first] / (arm + '-heads.npz')).unlink()
        self.save()
        with self.assertRaisesRegex(ValueError, 'comparison'):
            self.preflight()

    def test_invalid_evidence_is_rejected_before_runtime_import_or_output_creation(self):
        self.execution['status'] = 'INCOMPLETE execution; not a scientific refusal'
        self.save()
        output = self.root / 'reference-output'
        real_import = __import__
        def guarded_import(name, *args, **kwargs):
            if name in ('resource', 'dp5q_reference', 'tensorflow'):
                raise AssertionError('Preflight imported an expensive/POSIX-only runtime')
            return real_import(name, *args, **kwargs)
        argv = ['reference', '--manifest', str(self.manifest_path), '--owned', str(self.owned),
                '--source', str(self.root / 'unused'), '--output', str(output)]
        with patch.object(sys, 'argv', argv), patch('builtins.__import__', side_effect=guarded_import):
            with self.assertRaises(ValueError):
                reference.main()
        self.assertFalse(output.exists())

    def test_reference_completion_requires_every_comparison_and_explicit_skip(self):
        plan = self.preflight()
        comparisons = {(row['case_id'], row['arm']) for row in plan['comparisons']}
        skipped = {(row['case_id'], row['arm']): row['reason'] for row in plan['skips']}
        rows = [{'case_id': name, 'arms': {
            arm: {'prediction_status': 'PASS'} if (name, arm) in comparisons else
            {'prediction_status': 'not_run', 'reason': skipped[name, arm]} for arm in 'FA'
        }} for name in plan['case_ids']]
        self.assertEqual(reference.completed_comparisons(plan, rows), 2)
        with self.assertRaises(ValueError):
            reference.completed_comparisons(plan, rows[:-1])
        rows[0]['arms']['F']['prediction_status'] = 'not_run'
        with self.assertRaises(ValueError):
            reference.completed_comparisons(plan, rows)
        rows[0]['arms']['F']['prediction_status'] = 'PASS'
        del rows[-1]['arms']['F']
        with self.assertRaises(ValueError):
            reference.completed_comparisons(plan, rows)

    def test_pure_preflight_module_imports_without_posix_resource_or_oracle(self):
        real_import = __import__
        def guarded_import(name, *args, **kwargs):
            if name in ('resource', 'dp5q_reference', 'tensorflow'):
                raise AssertionError('Pure preflight import reached the reference runtime')
            return real_import(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=guarded_import):
            module = load_script('portable_reference_preflight', 'scripts/dp5q-reference/campaign.py')
        self.assertTrue(callable(module.preflight_owned))

    def test_full_mode_requires_the_exact_complete_370_case_set(self):
        self.run.update(mode='full',case_ids=[case['case_id'] for case in self.manifest['cases']])
        self.execution.update(self.run)
        self.execution.update(completed=370,case_paths={case['case_id']: f'case-{index:03d}' for index,case in enumerate(self.manifest['cases'])})
        self.save()
        with self.assertRaises(ValueError):
            self.preflight()
        for case in self.manifest['cases']:
            if case['case_id'] in self.results:
                continue
            folder=self.owned/self.execution['case_paths'][case['case_id']]
            folder.mkdir()
            (folder/'F.sdf').write_text('fixture geometry\n')
            (folder/'emitted.nmr').write_text('10(any)\n')
            self.results[case['case_id']]={key:case[key] for key in ('case_id','family_id','spectrum_id','source_label_correct')}
            self.results[case['case_id']]['arms']={arm:{'status':'not_applicable','reason':'strict_nmr_coverage_mismatch','score_raw':None,'log_score':None} for arm in 'FABC'}
        self.save()
        self.assertEqual(len(self.preflight()['case_ids']),370)

    def test_reference_output_inventory_requires_exact_files_counts_and_hashes(self):
        plan=self.preflight()
        output=self.root/'reference-output'
        output.mkdir()
        records={}
        expected={self.execution['case_paths'][name]+'/reference.json' for name in plan['case_ids']}
        expected.update(self.execution['case_paths'][row['case_id']]+'/'+row['arm']+'-legacy.npz' for row in plan['comparisons'])
        for relative in expected:
            path=output/relative
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b'tiny reference fixture')
            reference.record_reference_artifact(output,path,records)
        counts=reference.validate_reference_artifacts(output,plan,records)
        self.assertEqual(counts,{'reference_json_count':8,'legacy_array_count':2,'artifact_count':10})
        reduced=dict(records)
        del reduced[next(iter(reduced))]
        with self.assertRaises(ValueError):
            reference.validate_reference_artifacts(output,plan,reduced)
        first=output/next(iter(records))
        first.write_bytes(b'tampered')
        with self.assertRaises(ValueError):
            reference.validate_reference_artifacts(output,plan,records)


class OwnedBudgetTests(unittest.TestCase):
    """Simulate process completion/accounting using tiny files on every platform."""

    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.directory)
        self.manifest = ROOT / 'tests/fixtures/dp5q/manifest.json'
        self.cases = json.loads(self.manifest.read_text())['cases']
        self.ids = {case['case_id']: index for index, case in enumerate(self.cases)}
        self.output = self.root / 'output'
        self.args = SimpleNamespace(manifest=self.manifest, source=self.root / 'unused', output=self.output, full=False)
        self.usage = SimpleNamespace(ru_utime=0., ru_stime=0.)
        fake_resource = SimpleNamespace(RUSAGE_CHILDREN=1, getrusage=lambda _kind: self.usage)
        self.enterContext(patch.dict(sys.modules, {'resource': fake_resource}))
        self.enterContext(patch.object(evaluation.subprocess, 'run', return_value=SimpleNamespace(stdout='fixture-pin\n')))
        self.child = None

    def child_factory(self, *, live=False, wrong_identity=False, budget_marker=False):
        owner = self
        class Child:
            def __init__(self, *_args, **_kwargs):
                owner.child = self
                self.returncode = None if live else 0
                self.terminated = False
                self.reaped = False
                if not live:
                    for case_id in evaluation.PILOT_IDS:
                        folder = owner.output / f'case-{owner.ids[case_id]:03d}'
                        folder.mkdir()
                        (folder / 'result.json').write_text(json.dumps({'case_id': 'wrong' if wrong_identity else case_id}))
                if budget_marker:
                    (owner.output / 'budget-failure.json').write_text(json.dumps({'status': 'FAILED_BUDGET', 'reason': 'case reservation exhausted'}))
                    self.returncode = 1
            def poll(self): return self.returncode
            def terminate(self):
                self.terminated = True
                self.returncode = -15
            def kill(self): self.returncode = -9
            def wait(self, **_kwargs):
                self.reaped = True
                return self.returncode
        return Child

    def receipt(self):
        return json.loads((self.output / 'execution.json').read_text())

    def test_completed_child_over_total_budget_never_persists_pass(self):
        oversized = SimpleNamespace(is_file=lambda: True, stat=lambda: SimpleNamespace(st_size=512 * 1024 * 1024 + 1))
        original = Path.rglob
        def growing(path, pattern): return [oversized] if self.child is not None else original(path, pattern)
        with patch.object(evaluation.subprocess, 'Popen', self.child_factory()), patch.object(Path, 'rglob', growing):
            with self.assertRaises(ValueError):
                evaluation.run_campaign(self.args)
        self.assertEqual(self.receipt()['completed'], 8)
        self.assertTrue(self.receipt()['status'].startswith('FAILED_BUDGET'))
        self.assertLess(max(p.stat().st_size for p in self.output.rglob('*') if p.is_file()), 100_000)

    def test_live_budget_monitor_terminates_reaps_and_records_failure(self):
        calls = []
        def growing(_root):
            calls.append(True)
            return 0 if len(calls) == 1 else 512 * 1024 * 1024 + 1
        with patch.object(evaluation.subprocess, 'Popen', self.child_factory(live=True)), patch.object(evaluation, 'output_bytes', side_effect=growing):
            with self.assertRaises(ValueError):
                evaluation.run_campaign(self.args)
        self.assertTrue(self.child.terminated)
        self.assertTrue(self.child.reaped)
        self.assertTrue(self.receipt()['status'].startswith('FAILED_BUDGET'))

    def test_cpu_gate_and_wrong_result_identity_precede_pass(self):
        fake_resource = sys.modules['resource']
        fake_resource.getrusage = lambda _kind: self.usage
        def finish(*args, **kwargs):
            child = self.child_factory()(*args, **kwargs)
            self.usage = SimpleNamespace(ru_utime=36001., ru_stime=0.)
            return child
        with patch.object(evaluation.subprocess, 'Popen', side_effect=finish):
            with self.assertRaises(ValueError):
                evaluation.run_campaign(self.args)
        self.assertTrue(self.receipt()['status'].startswith('FAILED_BUDGET'))

    def test_wrong_case_identity_and_child_reservation_failure_cannot_pass(self):
        with patch.object(evaluation.subprocess, 'Popen', self.child_factory(wrong_identity=True)):
            with self.assertRaises(RuntimeError):
                evaluation.run_campaign(self.args)
        self.assertTrue(self.receipt()['status'].startswith('INCOMPLETE'))

    def test_child_reservation_marker_is_a_budget_failure(self):
        with patch.object(evaluation.subprocess, 'Popen', self.child_factory(budget_marker=True)):
            with self.assertRaises(ValueError):
                evaluation.run_campaign(self.args)
        self.assertTrue(self.receipt()['status'].startswith('FAILED_BUDGET'))

    def test_early_reservation_refuses_before_any_case_file_is_written(self):
        with patch.object(evaluation, 'output_bytes', return_value=512 * 1024 * 1024):
            with self.assertRaisesRegex(ValueError, 'budget|reservation'):
                evaluation.reserve_case_output(self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_completed_bounded_child_can_publish_pass(self):
        with patch.object(evaluation.subprocess, 'Popen', self.child_factory()):
            result = evaluation.run_campaign(self.args)
        self.assertEqual(result['status'], 'PASS owned campaign execution')
        self.assertEqual(result['completed'], 8)
        self.assertTrue(self.child.reaped)

    def test_case_publication_reserves_first_and_records_all_declared_artifacts(self):
        self.output.mkdir()
        with patch.object(evaluation, 'reserve_case_output', side_effect=evaluation.CampaignBudgetError('reservation full')), patch.object(evaluation, 'calculate_case') as calculate:
            with self.assertRaises(ValueError):
                evaluation.publish_case({'cases': [{}]}, 0, self.root, self.output, None)
        calculate.assert_not_called()
        self.assertFalse((self.output / 'case-000').exists())
        self.assertEqual(json.loads((self.output / 'budget-failure.json').read_text())['status'], 'FAILED_BUDGET')

    def test_bounded_case_result_contains_an_artifact_hash_inventory(self):
        self.output.mkdir()
        def calculate(_manifest, _case, _source, folder, _models):
            (folder / 'F.sdf').write_bytes(b'geometry')
            (folder / 'emitted.nmr').write_bytes(b'10(any)')
            return {'case_id': 'fixture'}
        with patch.object(evaluation, 'calculate_case', side_effect=calculate):
            evaluation.publish_case({'cases': [{}]}, 0, self.root, self.output, None)
        result = json.loads((self.output / 'case-000' / 'result.json').read_text())
        self.assertEqual(result['artifacts']['F.sdf'], {'bytes': 8, 'sha256': sha256(b'geometry').hexdigest()})
        self.assertFalse((self.output / 'case-000' / 'result.json.tmp').exists())

    def test_batch_entry_point_reaches_reservation_before_case_calculation(self):
        self.output.mkdir()
        fake = sys.modules['resource']
        fake.setrlimit = lambda *_args: None
        fake.RLIMIT_CPU, fake.RLIMIT_FSIZE, fake.RLIMIT_CORE = 1, 2, 3
        metadata = {'model_id': 'fixture', 'files': {'weights.npz': {'sha256': '0' * 64}}}
        network = SimpleNamespace(load_models=lambda: SimpleNamespace(metadata=metadata))
        argv = ['evaluate', '_batch', '--manifest', str(self.manifest), '--source', str(self.root),
                '--output', str(self.output), '--indices', '0']
        with patch.object(sys, 'argv', argv), patch.dict(sys.modules, {'checksi.dp5q.network': network}), patch.dict(os.environ, {}), patch('faulthandler.dump_traceback_later'), patch('faulthandler.cancel_dump_traceback_later'), patch.object(evaluation, 'reserve_case_output', side_effect=evaluation.CampaignBudgetError('reservation full')), patch.object(evaluation, 'calculate_case') as calculate:
            with self.assertRaises(ValueError):
                evaluation.main()
        calculate.assert_not_called()
        self.assertEqual(json.loads((self.output / 'budget-failure.json').read_text())['status'], 'FAILED_BUDGET')

    def test_wall_timeout_terminates_reaps_and_cannot_pass(self):
        with patch.object(evaluation.subprocess,'Popen',self.child_factory(live=True)), patch.object(evaluation.time,'monotonic',side_effect=[0.,601.]):
            with self.assertRaises(RuntimeError):
                evaluation.run_campaign(self.args)
        self.assertTrue(self.child.terminated and self.child.reaped)
        self.assertTrue(self.receipt()['timed_out'])
        self.assertTrue(self.receipt()['status'].startswith('INCOMPLETE'))

    def test_source_change_after_child_completion_cannot_pass(self):
        original=evaluation._bounded_bytes
        def changing(path,maximum):
            data=original(path,maximum)
            return data+b'changed' if self.child is not None and path==ROOT/'scripts/evaluate_dp5q.py' else data
        with patch.object(evaluation.subprocess,'Popen',self.child_factory()),patch.object(evaluation,'_bounded_bytes',side_effect=changing):
            with self.assertRaises(RuntimeError):
                evaluation.run_campaign(self.args)
        self.assertTrue(self.receipt()['status'].startswith('INCOMPLETE'))
        self.assertIn('source changed',self.receipt()['failure'])
