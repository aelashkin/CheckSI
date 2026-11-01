"""Real controlled children exercise run-scoped supervision and cleanup."""

import io
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from checksi.dp5q.client import DP5qCancelled, DP5qSession, DP5qWorkerError, DP5qWorkerUnavailable
from checksi.dp5q.contracts import CheckRequest, Frame

FIXTURE = Path(__file__).parent / "fixtures" / "dp5q" / "controlled_worker.py"


class WorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.availability = patch("checksi.dp5q.client.check_availability", return_value={
            "model_id": "fixture", "score_version": "score", "preprocessing_id": "graph",
            "geometry_policy": "single", "weights_sha256": "a" * 64})
        self.availability_mock = self.availability.start()
        self.addCleanup(self.availability.stop)

    def session(self, mode="normal", **kwargs):
        return DP5qSession(_command=[sys.executable, "-u", str(FIXTURE), mode], **kwargs)

    def request(self, name="A"):
        return CheckRequest(compound_id=name, smiles="CCO", carbon_shifts=(10.0, 20.0))

    def assert_clean(self, session):
        self.assertFalse(any(thread.name.startswith(session._thread_prefix) for thread in threading.enumerate()))
        self.assertTrue(all(process.poll() is not None for process in session._processes))

    def test_unused_session_starts_no_process(self):
        with self.session() as session:
            self.assertEqual(session.metadata, {})
        self.assertEqual(session._processes, [])

    def test_three_requests_reuse_one_child(self):
        with self.session() as session:
            results = [session.check(self.request(name)) for name in ("A", "B", "A")]
            self.assertEqual(len({result.provenance["pid"] for result in results}), 1)
            self.assertEqual(session.metadata["generation"], 1)
        self.assert_clean(session)

    def test_incomplete_and_contradictory_assessments_break_the_generation(self):
        for mode in ("missing_assignments", "malformed_assignment", "duplicate_observation",
                     "wrong_observed_value", "wrong_original_label", "wrong_provenance", "inconsistent_log_score"):
            with self.subTest(mode=mode):
                with self.session(mode) as session:
                    with self.assertRaises(DP5qWorkerError) as raised:
                        session.check(self.request())
                    self.assertEqual(raised.exception.reason, "protocol_error")
                self.assert_clean(session)

    def test_host_validates_fit_shape_without_repeating_the_workers_calculation(self):
        with self.session('contradictory_fit') as session:
            result = session.check(self.request())
            self.assertEqual(result.status, 'assessed')
            self.assertEqual(result.score_raw, .75)
            self.assertEqual(result.diagnostics['fit_locations'],
                             [[row['experimental_shift'] for row in result.assignments]])
            self.assertEqual(len(session._processes), 1)
        self.assert_clean(session)
        with self.session('missing_fit') as session:
            with self.assertRaises(DP5qWorkerError) as raised:
                session.check(self.request())
            self.assertEqual(raised.exception.reason, 'protocol_error')
            self.assertIn('fit', raised.exception.details.lower())
        self.assert_clean(session)

    def test_bad_terminal_is_not_retried_and_later_compound_can_restart(self):
        with self.session("wrong_observed_value_first") as session:
            with self.assertRaises(DP5qWorkerError):
                session.check(self.request("bad"))
            result = session.check(self.request("later"))
            self.assertEqual(result.compound_id, "later")
            self.assertEqual(result.provenance["generation"], 2)
        self.assert_clean(session)

    def test_result_scalars_diagnostics_and_all_ready_identifiers_are_checked(self):
        modes = ("boolean_index", "boolean_shift", "boolean_scaled", "bad_percent", "bad_percent_alias",
                 "missing_diagnostics", "missing_timings", "bad_regression_reason", "boolean_scaled_diagnostic",
                 "overflow_diagnostic", "missing_zero_reason", "invalid_zero_log", "null_positive_log",
                 "tiny_contradictory_score", "inconsistent_group_score") + tuple("wrong_id_" + name for name in
                     ("model_id", "score_version", "preprocessing_id", "geometry_policy", "weights_sha256"))
        for mode in modes:
            with self.subTest(mode=mode):
                with self.session(mode) as session:
                    with self.assertRaises(DP5qWorkerError) as raised:
                        session.check(self.request())
                    self.assertEqual(raised.exception.reason, "protocol_error")
                self.assert_clean(session)

    def test_valid_underflow_and_small_positive_scores_remain_assessed(self):
        for mode in ("display_underflow", "tail_beyond_float64", "tiny_positive_score"):
            with self.subTest(mode=mode):
                with self.session(mode) as session:
                    self.assertEqual(session.check(self.request()).status, "assessed")
                self.assert_clean(session)

    def test_known_inventory_groups_and_fixed_observations_are_checked(self):
        cases = [("wrong_inventory_label", self.request().model_copy(update={"carbon_labels": ("C1", "C2")})),
                 ("wrong_group_order", self.request().model_copy(update={"carbon_labels": ("C1", "C2")})),
                 ("wrong_fixed", self.request().model_copy(update={"fixed_assignments": {0: 1, 1: 0}})),
                 ("split_equivalence", CheckRequest(compound_id="grouped", smiles="CCC", carbon_shifts=(10.,20.),
                     carbon_labels=("C1", "C2", "C3"), carbon_groups=(("C1", "C3"),)))]
        for mode, request in cases:
            with self.subTest(mode=mode):
                with self.session(mode) as session:
                    with self.assertRaises(DP5qWorkerError):
                        session.check(request)
                self.assert_clean(session)

    def test_correct_inventory_equivalence_and_repeated_observations_pass(self):
        requests = [CheckRequest(compound_id="grouped", smiles="CCC", carbon_shifts=(10.,20.),
                        carbon_labels=("C1", "C2", "C3"), carbon_groups=(("C3", "C1"),)),
                    CheckRequest(compound_id="fixed", smiles="OCC", carbon_shifts=(20.,10.),
                        carbon_labels=("C2", "C3"), fixed_assignments={0: 1, 1: 0}),
                    CheckRequest(compound_id="repeated", smiles="CCO", carbon_shifts=(10.,10.),
                        carbon_labels=("C1", "C2"))]
        with self.session() as session:
            for request in requests:
                self.assertEqual(session.check(request).status, "assessed")
        self.assert_clean(session)

    def test_single_geometry_policy_rejects_multiple_conformers(self):
        self.availability_mock.return_value["geometry_policy"] = "converged-single-v1"
        with self.session("multiple_conformers_single") as session:
            with self.assertRaises(DP5qWorkerError) as raised:
                session.check(self.request())
            self.assertIn("single", raised.exception.details)
        self.assert_clean(session)

    def test_other_policy_does_not_equate_mean_absolute_error_with_absolute_mean(self):
        self.availability_mock.return_value["geometry_policy"] = "ensemble-fixture"
        request = CheckRequest(compound_id="population-errors", smiles="CCCO", carbon_shifts=(10.,20.,30.))
        with self.session("multiple_conformers_ensemble") as session:
            result = session.check(request)
            self.assertEqual(result.diagnostics["scaled_errors"], [0.,0.,0.])
            self.assertEqual(result.diagnostics["corrected_mae"], 4/3)
        self.assert_clean(session)

    def test_failed_compound_is_not_retried_but_later_compound_can_restart(self):
        with self.session("crash_first") as session:
            with self.assertRaises(DP5qWorkerError):
                session.check(self.request("failed"))
            result = session.check(self.request("later"))
            self.assertEqual(result.compound_id, "later")
            self.assertEqual(result.provenance["generation"], 2)
        self.assert_clean(session)

    def test_second_process_failure_exhausts_restart(self):
        with self.session("crash") as session:
            with self.assertRaises(DP5qWorkerError):
                session.check(self.request("first"))
            with self.assertRaises(DP5qWorkerUnavailable):
                session.check(self.request("second"))
            with self.assertRaises(DP5qWorkerUnavailable):
                session.check(self.request("third"))
        self.assertEqual(len(session._processes), 2)
        self.assert_clean(session)

    def test_two_compound_timeouts_leave_the_crash_replacement_available(self):
        with self.session("timeout_compounds", settings=SimpleNamespace(timeout_seconds=.1)) as session:
            for name in ("slowA", "slowB"):
                with self.assertRaises(DP5qWorkerError) as raised:
                    session.check(self.request(name))
                self.assertEqual(raised.exception.reason, "worker_timeout")
            self.assertEqual(session._failures, 0)
            self.assertEqual(session.check(self.request("later")).status, "assessed")
        self.assert_clean(session)

    def test_parent_validation_timeouts_leave_later_healthy_generations_available(self):
        from checksi.dp5q.contracts import validate_result_for_request

        def delayed_validation(request, result, metadata, *, cancel_check):
            # A healthy terminal arrived; only the parent validation is now late.
            expired = time.monotonic() + session.timeout + 1
            with patch('checksi.dp5q.client.time.monotonic', return_value=expired):
                return validate_result_for_request(request, result, metadata, cancel_check=cancel_check)

        with self.session(settings=SimpleNamespace(timeout_seconds=2)) as session:
            for name in ('slow-parent-A', 'slow-parent-B'):
                with patch('checksi.dp5q.client.validate_result_for_request',
                           side_effect=delayed_validation):
                    with self.assertRaises(DP5qWorkerError) as raised:
                        session.check(self.request(name))
                self.assertEqual(raised.exception.reason, 'request_timeout')
                self.assertFalse(raised.exception.count_crash)
                self.assertEqual(session._failures, 0)
                self.assertFalse(session._unavailable)
                self.assertIsNone(session._generation)
                self.assert_clean(session)
            result = session.check(self.request('healthy-later'))
            self.assertEqual(result.status, 'assessed')
            self.assertEqual(result.provenance['generation'], 3)
        self.assert_clean(session)

    def test_constructed_invalid_request_is_revalidated_without_a_process_or_crash_charge(self):
        with self.session() as session:
            invalid = self.request().model_copy(update={"carbon_shifts": (True,)})
            with self.assertRaises(DP5qWorkerError) as raised:
                session.check(invalid)
            self.assertEqual(raised.exception.reason, "invalid_request")
            self.assertEqual(session._processes, [])
            self.assertEqual(session._failures, 0)
            self.assertEqual(session.check(self.request()).status, "assessed")
        self.assert_clean(session)

    def test_local_encode_and_result_validation_failures_do_not_charge_crash_budget(self):
        with self.session("wrong_observed_value") as session:
            for _ in range(3):
                with self.assertRaises(DP5qWorkerError):
                    session.check(self.request())
            self.assertEqual(session._failures, 0)
            self.assertFalse(session._unavailable)
        self.assert_clean(session)
        with self.session() as session:
            session.check(self.request())
            with patch.object(session._generation, "send", side_effect=ValueError("local encode failure")):
                with self.assertRaises(DP5qWorkerError) as raised:
                    session.check(self.request())
            self.assertEqual(raised.exception.reason, "invalid_request")
            self.assertEqual(session._failures, 0)
            self.assertEqual(session.check(self.request()).status, "assessed")
            self.assertEqual(len(session._processes), 1)
        self.assert_clean(session)

    def test_worker_rejection_preserves_cause_locally_without_spending_crash_replacement(self):
        with self.session("request_rejected") as session:
            with self.assertRaises(DP5qWorkerError) as raised:
                session.check(self.request())
            self.assertEqual(raised.exception.reason, "protocol_error")
            self.assertIn("controlled request schema rejection", raised.exception.diagnostic_detail)
            self.assertEqual(session._failures, 0)
        self.assert_clean(session)

    def test_idle_death_replaces_before_sending_the_next_unattempted_request(self):
        with self.session() as session:
            session.check(self.request("A"))
            first = session._generation.process
            first.kill()
            first.wait(timeout=2)
            result = session.check(self.request("B"))
            self.assertEqual(result.compound_id, "B")
            self.assertEqual(result.provenance["generation"], 2)
            self.assertEqual(session._sequence, 2)
            second = session._generation.process
            second.kill()
            second.wait(timeout=2)
            with self.assertRaises(DP5qWorkerUnavailable) as unavailable:
                session.check(self.request("C"))
            self.assertEqual(unavailable.exception.cause, "worker_exited")
            self.assertEqual(unavailable.exception.returncode, -9 if sys.platform != "win32" else 1)
            self.assertIn("worker exit status", str(unavailable.exception))
            self.assertEqual(session._sequence, 2, "an unavailable request must never be sent")
        self.assertEqual(len(session._processes), 2)
        self.assert_clean(session)

    def test_startup_failure_is_terminal(self):
        with self.session("startup_error") as session:
            with self.assertRaises(DP5qWorkerUnavailable) as raised:
                session.check(self.request())
            self.assertNotIn("/private/worker-test", str(raised.exception))
            self.assertIn("/private/worker-test", raised.exception.stderr_tail)
            with self.assertRaises(DP5qWorkerUnavailable):
                session.check(self.request("later"))
        self.assertEqual(len(session._processes), 1)
        self.assert_clean(session)

    def test_startup_timeout_is_terminal_and_reaped(self):
        with self.session("startup_stall", startup_timeout=0.15) as session:
            with self.assertRaises(DP5qWorkerUnavailable):
                session.check(self.request())
        self.assert_clean(session)

    def test_reader_thread_start_failure_still_reaps_the_started_process(self):
        session = self.session()
        original_start = threading.Thread.start
        started = []
        def start(thread):
            if thread.name.startswith(session._thread_prefix):
                started.append(thread)
                if len(started) == 2:
                    raise RuntimeError("controlled thread creation failure")
            return original_start(thread)
        try:
            with patch.object(threading.Thread, "start", start):
                with self.assertRaises(DP5qWorkerUnavailable):
                    session.check(self.request())
            self.assert_clean(session)
        finally:
            # The reproduction also cleans up the pre-fix ownership failure.
            for process in session._processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
            for thread in started:
                if thread.ident is not None:
                    thread.join(timeout=1)
            session.close()

    def test_request_deadline_is_not_reset_by_progress_or_partial_frames(self):
        for mode in ("stall", "partial", "progress_until_deadline"):
            with self.subTest(mode=mode):
                with self.session(mode, settings=SimpleNamespace(timeout_seconds=0.1)) as session:
                    started = time.monotonic()
                    with self.assertRaises(DP5qWorkerError) as raised:
                        session.check(self.request())
                    self.assertEqual(raised.exception.reason, "worker_timeout")
                    self.assertLess(time.monotonic() - started, 2)
                self.assert_clean(session)

    def test_terminal_wakeup_after_absolute_deadline_is_rejected(self):
        session = self.session()
        clock = [0.0]

        def late_terminal(*, timeout):
            # Simulate the parent waking after its deadline with a queued frame.
            clock[0] = 2.0
            return Frame(kind="result", run_id="fixture", generation=1, sequence=1)

        session._generation = SimpleNamespace(
            failed=threading.Event(), lock=threading.Lock(), progress=None,
            responses=SimpleNamespace(get=late_terminal),
        )
        try:
            with patch("checksi.dp5q.client.time.monotonic", side_effect=lambda: clock[0]):
                with self.assertRaises(DP5qWorkerError) as raised:
                    session._wait("result", deadline=1.0)
            self.assertEqual(raised.exception.reason, "worker_timeout")
        finally:
            session._generation = None
            session.close()
        self.assert_clean(session)

    def test_eof_preserves_normal_exit_during_bounded_interpreter_teardown(self):
        class FinishingProcess:
            returncode = None
            terminated = False

            def __init__(self):
                self.stdin, self.stdout, self.stderr = (io.BytesIO() for _ in range(3))
                self.wait_timeouts = []

            def poll(self):
                return self.returncode

            def wait(self, timeout):
                self.wait_timeouts.append(timeout)
                if self.returncode is None:
                    if timeout < 0.25:
                        raise subprocess.TimeoutExpired("finishing interpreter", timeout)
                    self.returncode = 0
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = -15

        process = FinishingProcess()
        with self.session() as session:
            session._generation = SimpleNamespace(process=process, stop=threading.Event(), threads=[])
            session._stop_generation(wait_for_exit=True)
        self.assertEqual(process.returncode, 0)
        self.assertFalse(process.terminated)
        self.assertTrue(all(0 < timeout <= 2 for timeout in process.wait_timeouts))
        self.assertTrue(all(stream.closed for stream in (process.stdin, process.stdout, process.stderr)))
        self.assert_clean(session)

    def test_malformed_stale_duplicate_and_mismatched_responses_fail(self):
        for mode in ("malformed", "nonfinite", "oversized", "partial_eof", "stale", "wrong_run",
                     "wrong_generation", "wrong_version", "wrong_compound", "duplicate", "bad_ready"):
            with self.subTest(mode=mode):
                with self.session(mode) as session:
                    with self.assertRaises(DP5qWorkerError):
                        session.check(self.request())
                self.assert_clean(session)

    def test_stderr_flood_is_drained_into_bounded_diagnostics(self):
        with self.session("stderr_flood") as session:
            with self.assertRaises(DP5qWorkerError) as raised:
                session.check(self.request())
            self.assertIn("FINAL-STDERR-MARKER", raised.exception.stderr_tail)
            self.assertIn("/private/worker-test/input.docx", raised.exception.stderr_tail)
            self.assertNotIn("FINAL-STDERR-MARKER", str(raised.exception))
            self.assertNotIn("/private/worker-test", raised.exception.details)
            self.assertLessEqual(len(raised.exception.stderr_tail.encode()), 64 * 1024)
            self.assertLess(len(str(raised.exception)), 2048)
            with self.assertRaises(DP5qWorkerUnavailable) as unavailable:
                session.check(self.request("later"))
            self.assertIn("FINAL-STDERR-MARKER", unavailable.exception.stderr_tail)
            self.assertNotIn("/private/worker-test", str(unavailable.exception))
        self.assert_clean(session)

    def test_short_actual_child_traceback_stays_out_of_error_details(self):
        def confirm_drain(event):
            if event.message.endswith(": short-stderr-ready"):
                deadline = time.monotonic() + 1
                while "controlled short traceback" not in session._generation.stderr_tail() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertIn("controlled short traceback", session._generation.stderr_tail())
                session._generation.process.stdin.close()
        with self.session("short_stderr_fail", event_dispatcher=confirm_drain) as session:
            with self.assertRaises(DP5qWorkerError) as raised:
                session.check(self.request())
            error = raised.exception
            self.assertIn("controlled short traceback", error.stderr_tail)
            self.assertIn("/private/worker-test/secret.py", error.stderr_tail)
            for marker in ("Traceback", "/private/worker-test", "controlled short traceback"):
                self.assertNotIn(marker, error.details)
                self.assertNotIn(marker, str(error))
        self.assert_clean(session)

    def test_huge_timing_and_unsafe_result_text_fail_promptly_without_reader_traceback(self):
        for mode in ("huge_timing", "integer_sdf", "unexpected_sdf", "malformed_sdf", "nul_message", "oversized_message"):
            with self.subTest(mode=mode), self.session(mode, settings=SimpleNamespace(timeout_seconds=2)) as session:
                started = time.monotonic()
                with self.assertRaises(DP5qWorkerError) as raised:
                    session.check(self.request().model_copy(update={"include_geometry": mode != "unexpected_sdf"}))
                self.assertEqual(raised.exception.reason, "protocol_error")
                self.assertLess(time.monotonic() - started, 1.5)
            self.assert_clean(session)

    def test_each_result_validator_branch_rejects_its_own_coherent_counterexample(self):
        modes = ("inconsistent_timing", "unassessed_rows", "unassessed_message", "negative_observation",
                 "percent_over_100", "incorrect_corrected_mae", "degenerate_regression", "invalid_crossings",
                 "unnormalized_population", "unordered_geometry_counts")
        for mode in modes:
            with self.subTest(mode=mode), self.session(mode) as session:
                request = self.request()
                if mode == "degenerate_regression":
                    request = request.model_copy(update={"carbon_shifts": (10., 10.)})
                with self.assertRaises(DP5qWorkerError) as raised:
                    session.check(request)
                self.assertEqual(raised.exception.reason, "protocol_error")
            self.assert_clean(session)

    def test_final_kill_timeout_is_typed_and_keeps_unreaped_generation_owned(self):
        class DelayedProcess:
            returncode = None
            reaping_allowed = False

            def __init__(self):
                self.stdin, self.stdout, self.stderr = (io.BytesIO() for _ in range(3))
                self.signals = []
                self.wait_timeouts = []

            def poll(self):
                return self.returncode

            def wait(self, timeout):
                self.wait_timeouts.append(timeout)
                if not self.reaping_allowed:
                    raise subprocess.TimeoutExpired("delayed reap", timeout)
                self.returncode = -9
                return self.returncode

            def terminate(self):
                self.signals.append("terminate")

            def kill(self):
                self.signals.append("kill")

        process = DelayedProcess()
        session = self.session(cancel_grace=0)
        generation = SimpleNamespace(process=process, stop=threading.Event(), threads=[],
                                     stderr_tail=lambda: "local diagnostic", number=1, sequence=0,
                                     send=lambda frame: None)
        session._generation = generation
        session._processes.append(process)
        try:
            with self.assertRaises(DP5qWorkerUnavailable) as raised:
                session.close()
            self.assertEqual(raised.exception.reason, "worker_cleanup_failed")
            self.assertIn("unconfirmed", raised.exception.details)
            self.assertIs(session._generation, generation)
            self.assertTrue(session._unavailable)
            self.assertTrue(generation.stop.is_set())
            self.assertEqual(process.signals, ["terminate", "kill"])
            self.assertTrue(all(stream.closed for stream in (process.stdin, process.stdout, process.stderr)))
            process.reaping_allowed = True
            session.close()
            self.assertIsNone(session._generation)
            self.assertTrue(all(timeout <= 1 for timeout in process.wait_timeouts))
        finally:
            process.reaping_allowed = True
            session.close()

    def test_cleanup_failure_preserves_the_original_timeout_and_local_cause(self):
        from unittest.mock import Mock
        process = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO(),
            poll=Mock(return_value=None), wait=Mock(side_effect=subprocess.TimeoutExpired("fixture", 1)),
            terminate=Mock(), kill=Mock())
        session = self.session(cancel_grace=0)
        session._generation = SimpleNamespace(process=process, stop=threading.Event(), threads=[],
            stderr_tail=lambda: "local stderr cause")
        original = DP5qWorkerError("worker_timeout", "Original compound deadline expired.", count_crash=False)
        try:
            with self.assertRaises(DP5qWorkerUnavailable) as raised:
                session._stop_after_error(original)
            self.assertEqual(raised.exception.cause, "worker_timeout")
            self.assertIs(raised.exception.__cause__, original)
            self.assertIn("Original compound deadline", raised.exception.diagnostic_detail)
            self.assertIn("local stderr cause", raised.exception.stderr_tail)
        finally:
            process.poll.return_value = -9
            process.wait.side_effect = None
            process.wait.return_value = -9
            session.close()

    def test_progress_flood_does_not_block_terminal_delivery(self):
        events = []
        with self.session("progress_flood", event_dispatcher=events.append) as session:
            self.assertEqual(session.check(self.request()).score_raw, 0.75)
        self.assertLess(len(events), 15000)
        self.assert_clean(session)

    def test_cancel_in_startup_busy_and_blocked_write_reaps_without_restart(self):
        large = CheckRequest(compound_id="large", smiles="CC", carbon_shifts=(10.0,),
            carbon_groups=(("C" + "1" * 400000, "C" + "2" * 400000),))
        for mode in ("startup_stall", "stall", "never_read", "partial_read"):
            with self.subTest(mode=mode):
                token = threading.Event()
                with self.session(mode, cancel_token=token, cancel_grace=0.1) as session:
                    timer = threading.Timer(0.25, token.set)
                    timer.start()
                    try:
                        started = time.monotonic()
                        with self.assertRaises(DP5qCancelled):
                            session.check(large if "read" in mode else self.request())
                        self.assertLess(time.monotonic() - started, 5)
                        with self.assertRaises(DP5qCancelled):
                            session.check(self.request("later"))
                    finally:
                        timer.cancel()
                        timer.join()
                self.assertEqual(len(session._processes), 1)
                self.assert_clean(session)

    def test_cancellation_before_first_check_and_while_ready_starts_no_replacement(self):
        token = threading.Event()
        token.set()
        with self.session(cancel_token=token) as session:
            with self.assertRaises(DP5qCancelled):
                session.check(self.request())
        self.assertEqual(session._processes, [])
        token.clear()
        with self.session(cancel_token=token) as session:
            session.check(self.request())
            token.set()
            with self.assertRaises(DP5qCancelled):
                session.check(self.request("later"))
        self.assertEqual(len(session._processes), 1)
        self.assert_clean(session)

    @unittest.skipIf(sys.platform == "win32", "SIGTERM ignore exercises POSIX kill escalation")
    def test_default_cancellation_grace_then_kill_completes_under_five_seconds(self):
        token = threading.Event()
        cancelled_at = []
        def cancel_after_native_block(event):
            cancelled_at.append(time.monotonic())
            token.set()
        with self.session("ignore_terminate", cancel_token=token,
                          event_dispatcher=cancel_after_native_block) as session:
            with self.assertRaises(DP5qCancelled):
                session.check(self.request())
            self.assertLess(time.monotonic() - cancelled_at[0], 5)
        self.assertEqual(session._processes[0].returncode, -9)
        self.assert_clean(session)


if __name__ == "__main__":
    unittest.main()
