"""The native packaging probe cancels an eligible current geometry request."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest

from rdkit import Chem

from checksi.dp5q.assignment import AssignmentError
from checksi.dp5q.contracts import CheckRequest
from checksi.dp5q.observation_assignment import validate_observation_coverage
from checksi.dp5q.observation_policy import derive_observation_policy
from checksi.events import ProgressEvent


class NativeCancellationFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / 'scripts/dp5q_packaging_check.py'
        spec = importlib.util.spec_from_file_location('native_cancel_packaging', path)
        cls.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.helper)

    def assert_eligible(self, request):
        policy = derive_observation_policy(Chem.MolFromSmiles(request.smiles), request.carbon_groups)
        self.assertTrue(policy.partition_complete)
        self.assertEqual(len(policy.groups), 30)
        self.assertEqual([group.labels for group in policy.groups], [(f'C{index + 1}',) for index in range(30)])
        validate_observation_coverage(len(policy.groups), len(request.carbon_shifts),
            observation_atom_counts=request.observation_atom_counts,
            coincident_environments=request.coincident_environments, fixed=request.fixed_assignments)

    def test_old_symmetric_fixture_refuses_before_prediction_geometry(self):
        request = CheckRequest(compound_id='old-cancel-native', smiles='C' * 30, carbon_shifts=list(range(30)))
        with self.assertRaises(AssignmentError) as caught:
            derive_observation_policy(Chem.MolFromSmiles(request.smiles), request.carbon_groups)
        self.assertEqual(caught.exception.reason, 'source_partition_mismatch')

    def test_asymmetric_engineering_fixture_has_complete_unique_carbon_coverage(self):
        self.assert_eligible(CheckRequest(compound_id='candidate', smiles='C' * 30 + 'O',
                                          carbon_shifts=list(range(30))))

    def test_helper_request_uses_the_eligible_stress_fixture(self):
        self.assert_eligible(self.helper._native_cancel_request())

    def scope(self):
        process = SimpleNamespace(pid=101, poll=lambda: None)
        generation = SimpleNamespace(sequence=1, terminal_sequence=1, progress='geometry',
            lock=threading.Lock(), failed=threading.Event(), ready_seen=True,
            process=process, run_id='owned-run', number=1)
        session = SimpleNamespace(_generation=generation, _sequence=1,
                                  metadata={'run_id': 'owned-run', 'generation': 1})
        observation = SimpleNamespace(pid=101, is_running=lambda: True)
        return session, generation, observation, threading.Event()

    def test_warmup_progress_is_drained_and_only_current_geometry_cancels(self):
        session, generation, observation, token = self.scope()
        callback, state = self.helper._cancel_on_geometry(session, token, observation, 'current')
        self.assertIsNone(generation.progress)
        callback(ProgressEvent(0, 0, 'DP5q: geometry'))
        self.assertFalse(token.is_set())  # Old sequence remains warmup, never current work.
        session._sequence = generation.sequence = 2
        callback(ProgressEvent(0, 0, 'DP5q: coverage'))
        callback(SimpleNamespace(message='DP5q: geometry'))
        self.assertFalse(token.is_set())
        callback(ProgressEvent(0, 0, 'DP5q: geometry'))
        self.assertTrue(token.is_set())
        self.assertEqual(state['geometry_progress']['request_sequence'], 2)
        self.assertEqual(state['geometry_progress']['terminal_sequence'], 1)
        self.assertTrue(state['geometry_progress']['worker_alive'])
        self.assertTrue(state['geometry_progress']['request_inflight'])
        self.assertEqual(state['compound_id'], 'current')

    def test_completed_or_dead_request_cannot_supply_cancellation_proof(self):
        for failure in ('terminal', 'launcher-exit', 'interpreter-exit', 'generation-failed', 'generation-replaced'):
            with self.subTest(failure=failure):
                session, generation, observation, token = self.scope()
                callback, state = self.helper._cancel_on_geometry(session, token, observation, 'current')
                session._sequence = generation.sequence = 2
                if failure == 'terminal':
                    generation.terminal_sequence = 2
                elif failure == 'launcher-exit':
                    generation.process.poll = lambda: 0
                elif failure == 'interpreter-exit':
                    observation.is_running = lambda: False
                elif failure == 'generation-failed':
                    generation.failed.set()
                else:
                    session._generation = None
                with self.assertRaises(AssertionError):
                    callback(ProgressEvent(0, 0, 'DP5q: geometry'))
                self.assertFalse(token.is_set())
                self.assertNotIn('cancel_requested_at', state)


if __name__ == '__main__':
    unittest.main()
