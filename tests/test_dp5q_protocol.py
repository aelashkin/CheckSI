"""Child commands and real bundled models exercise protocol/control paths."""

import builtins
from contextlib import ExitStack
import io
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import checksi.dp5q
from checksi.dp5q.client import (DP5qCancelled, DP5qSession, DP5qWorkerError,
                                    DP5qWorkerUnavailable, doctor_runtime)
from checksi.dp5q.contracts import CheckRequest, Frame, MAX_FRAME_BYTES, encode_frame
from checksi.dp5q.worker import _CommandReader, _Output, _serve
from tests.dp5q_process_observation import ProcessEscalationObservation


class OutputCollector:
    def __init__(self):
        self.frames = []

    def send(self, kind, sequence, payload=None):
        self.frames.append((kind, sequence, payload))
        sent = threading.Event()
        sent.set()
        return sent


class ChildProtocolTests(unittest.TestCase):
    def reader(self, raw, *, ready=True):
        output = OutputCollector()
        reader = _CommandReader(io.BytesIO(raw), "run", 1, output)
        reader.ready = ready
        reader.read()
        return reader, output

    def frame(self, kind="check", sequence=1, **changes):
        payload = {"compound_id": "A", "smiles": "CC", "carbon_shifts": [10.0, 20.0]} if kind == "check" else {}
        return encode_frame(Frame(kind=kind, run_id="run", generation=1,
            sequence=sequence, payload=payload, **changes))

    def test_valid_request_is_typed_and_eof_cancels_further_work(self):
        reader, output = self.reader(self.frame())
        frame, request = reader.commands.get_nowait()
        self.assertEqual(frame.sequence, 1)
        self.assertEqual(request.carbon_shifts, (10.0, 20.0))
        self.assertTrue(reader.cancel.is_set())
        self.assertTrue(reader.stopping.is_set())
        self.assertEqual(output.frames, [])

    def test_invalid_malformed_partial_oversized_and_wrong_ids_are_fatal(self):
        invalid = [b"bad\n", b'{"version":1', b"x" * (MAX_FRAME_BYTES + 1),
            self.frame().replace(b'"run_id":"run"', b'"run_id":"other"'),
            self.frame().replace(b'"generation":1', b'"generation":2'),
            self.frame().replace(b'"version":2', b'"version":1'),
            self.frame().replace(b'[10.0,20.0]', b'[NaN,20.0]')]
        for raw in invalid:
            with self.subTest(prefix=raw[:50]):
                reader, output = self.reader(raw)
                self.assertTrue(reader.stopping.is_set())
                self.assertEqual(output.frames[0][0], "error")
                self.assertEqual(output.frames[0][2]["reason"], "protocol_error")

    def test_request_before_ready_and_pipelined_duplicate_are_fatal(self):
        _, output = self.reader(self.frame(), ready=False)
        self.assertEqual(output.frames[0][2]["reason"], "protocol_error")
        for next_sequence in (1, 2):
            _, output = self.reader(self.frame() + self.frame(sequence=next_sequence))
            self.assertEqual(output.frames[0][2]["reason"], "protocol_error")

    def test_request_rejection_uses_the_offending_sequence_and_keeps_its_cause(self):
        raw = self.frame(sequence=7).replace(b'[10.0,20.0]', b'[true,20.0]')
        _, output = self.reader(raw)
        self.assertEqual(output.frames[0][1], 7)
        self.assertIn("finite", output.frames[0][2]["details"])

    def test_cancel_and_shutdown_remain_readable_after_a_request(self):
        for control in ("cancel", "shutdown"):
            with self.subTest(control=control):
                reader, output = self.reader(self.frame() + self.frame(control))
                self.assertTrue(reader.cancel.is_set())
                self.assertEqual(output.frames[0][:2], ("closed", 1))

    def test_progress_pressure_preserves_terminal_capacity_without_blocking(self):
        entered, release, stopping = threading.Event(), threading.Event(), threading.Event()
        class BlockedStream(io.BytesIO):
            def write(self, raw):
                entered.set()
                if not release.wait(2):
                    raise OSError("test write deadline")
                return super().write(raw)
        stream = BlockedStream()
        output = _Output(stream, "run", 1, stopping)
        try:
            output.send("ready", 0)
            self.assertTrue(entered.wait(1))
            for _ in range(15000):
                output.progress(1, "inference")
            sent = output.send("result", 1, {"compound_id": "A", "status": "assessed", "score_raw": 0.75})
            self.assertFalse(stopping.is_set())
            self.assertEqual(output.terminals.qsize(), 1)
            release.set()
            self.assertTrue(sent.wait(1))
        finally:
            release.set()
            output.finish()
        lines = stream.getvalue().splitlines()
        self.assertLessEqual(len(lines), 3)
        self.assertIn(b'"kind":"result"', lines[-1])

    def test_terminal_overflow_uses_a_nonblocking_fatal_signal(self):
        entered, release, stopping = threading.Event(), threading.Event(), threading.Event()
        class BlockedStream(io.BytesIO):
            def write(self, raw):
                entered.set()
                if not release.wait(2):
                    raise OSError("test write deadline")
                return super().write(raw)
        output = _Output(BlockedStream(), "run", 1, stopping)
        try:
            output.send("ready", 0)
            self.assertTrue(entered.wait(1))
            output.send("result", 1)
            output.send("closed", 1)
            output.send("error", 1)
            self.assertTrue(stopping.is_set())
            self.assertEqual(output.terminals.qsize(), 2)
        finally:
            release.set()
            output.finish()


class ChildStartupTests(unittest.TestCase):
    """Simulate native DLL initialization inspecting a pipe with a pending read."""

    def test_native_imports_finish_before_background_stdin_read(self):
        reading, release = threading.Event(), threading.Event()
        imports = []
        model = SimpleNamespace(metadata={"model_id": "model", "score_id": "score",
            "preprocessing_id": "graph", "geometry_policy": "single",
            "files": {"weights.npz": {"sha256": "a" * 64}}})

        class PipeInput:
            def read(self, size):
                reading.set()
                if not release.wait(2):
                    raise OSError("test pipe read deadline")
                return b""

        original_import = builtins.__import__
        original_start = threading.Thread.start

        def start(thread):
            original_start(thread)
            if thread.name == "dp5q-input":
                self.assertTrue(reading.wait(1))

        def native_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name in ("network", "engine") and level == 1:
                imports.append(name)
                if reading.is_set():
                    release.set()
                    raise RuntimeError("native DLL initialization blocked behind stdin read")
                if name == "network":
                    return SimpleNamespace(load_models=lambda: model)
                release.set()
                return SimpleNamespace(check_compound=lambda *values, **options: None)
            return original_import(name, globals, locals, fromlist, level)

        try:
            with patch.object(threading.Thread, "start", start), patch.object(builtins, "__import__", native_import):
                self.assertEqual(_serve(PipeInput(), io.BytesIO(), "run", 1), 0)
            self.assertEqual(imports, ["network", "engine"])
            self.assertTrue(reading.is_set())
        finally:
            release.set()


class CancellationExitTests(unittest.TestCase):
    """Pin allowed cancellation exits to actual parent-owned escalation."""

    def test_unexplained_abort_sigint_and_unmatched_signals_are_rejected(self):
        cases = [(None, []), (-6, []), (-2, []), (-15, []), (-9, []), (1, []),
                 (-6, ["terminate", "kill"]), (-2, ["terminate", "kill"]),
                 (-9, ["terminate"]), (7, ["terminate", "kill"])]
        if os.name == "posix":
            cases.append((-signal.SIGTERM, ["kill"]))
        for returncode, signals in cases:
            with self.subTest(returncode=returncode, signals=signals):
                observation = ProcessEscalationObservation(SimpleNamespace(poll=lambda: returncode))
                observation.signals = signals
                with self.assertRaises(AssertionError):
                    observation.assert_cancelled_exit(self)

    def test_recorded_parent_escalations_and_cooperative_exit_are_accepted(self):
        cases = [(0, [])]
        if os.name == "posix":
            cases.extend([(-signal.SIGTERM, ["terminate"]), (-signal.SIGKILL, ["terminate", "kill"])])
        else:
            cases.extend([(1, ["terminate"]), (1, ["kill"])])
        for returncode, signals in cases:
            with self.subTest(returncode=returncode, signals=signals):
                observation = ProcessEscalationObservation(SimpleNamespace(poll=lambda: returncode))
                observation.signals = signals
                observation.assert_cancelled_exit(self)

    def controlled_session(self, mode="normal", **kwargs):
        fixture = Path(__file__).parent / "fixtures/dp5q/controlled_worker.py"
        return DP5qSession(_command=[sys.executable, "-u", str(fixture), mode], **kwargs)

    def availability(self):
        return patch("checksi.dp5q.client.check_availability", return_value={
            "model_id": "fixture", "score_version": "score", "preprocessing_id": "graph",
            "geometry_policy": "single", "weights_sha256": "a" * 64})

    def assert_reaped(self, session):
        self.assertTrue(all(process.poll() is not None for process in session._processes))
        self.assertFalse(any(thread.name.startswith(session._thread_prefix) for thread in threading.enumerate()))

    def test_controlled_cooperative_cancellation_has_zero_exit_without_escalation(self):
        token = threading.Event()
        request = CheckRequest(compound_id="A", smiles="CCO", carbon_shifts=(10., 20.))
        with self.availability(), self.controlled_session(cancel_token=token) as session:
            session.check(request)
            with ProcessEscalationObservation(session._generation.process) as observation:
                token.set()
                with self.assertRaises(DP5qCancelled):
                    session.check(request)
                self.assertEqual(observation.signals, [])
                self.assertEqual(observation.process.poll(), 0)
                observation.assert_cancelled_exit(self)
        self.assert_reaped(session)

    @unittest.skipUnless(os.name == "posix", "POSIX SIGTERM ignore forces parent kill")
    def test_controlled_blocked_child_accepts_only_observed_parent_kill(self):
        token = threading.Event()
        observations = []
        with ExitStack() as stack:
            def cancel(event):
                if not observations and event.message.endswith(": native-block"):
                    observations.append(stack.enter_context(ProcessEscalationObservation(session._generation.process)))
                    token.set()
            with self.availability(), self.controlled_session("ignore_terminate", cancel_token=token,
                                                             event_dispatcher=cancel) as session:
                with self.assertRaises(DP5qCancelled):
                    session.check(CheckRequest(compound_id="A", smiles="CC", carbon_shifts=(10., 20.)))
                self.assertEqual(len(observations), 1)
                observation = observations[0]
                self.assertEqual(observation.signals, ["terminate", "kill"])
                self.assertEqual(observation.process.poll(), -signal.SIGKILL)
                observation.assert_cancelled_exit(self)
            self.assert_reaped(session)


class RealChildProtocolTests(unittest.TestCase):
    """Exercise actual bundled weights and engine in the production child."""

    def request(self, name="ethanol"):
        return CheckRequest(compound_id=name, smiles="CCO", carbon_shifts=(18.5, 58.0))

    def long_request(self):
        #An asymmetric carbon chain has20 true environments and reaches native work.
        return CheckRequest(compound_id="native-control", smiles="C" * 20 + "O",
                            carbon_shifts=tuple(float(index + 10) for index in range(20)))

    def assert_reaped(self, session):
        self.assertTrue(all(process.poll() is not None for process in session._processes))
        self.assertFalse(any(thread.name.startswith(session._thread_prefix) for thread in threading.enumerate()))

    def test_actual_models_serve_a_b_a_with_one_ready_generation(self):
        with DP5qSession() as session:
            first = session.check(self.request())
            middle = session.check(CheckRequest(compound_id="benzene", smiles="c1ccccc1",
                carbon_shifts=(128.5,), carbon_groups=(("C1", "C2", "C3", "C4", "C5", "C6"),)))
            again = session.check(self.request())
            self.assertEqual([first.status, middle.status, again.status], ["assessed"] * 3)
            self.assertEqual(first.score_raw, again.score_raw)
            self.assertEqual(first.log_score, again.log_score)
            self.assertEqual(len(session._processes), 1)
            self.assertEqual(session.metadata["generation"], 1)
            self.assertEqual(len(session.metadata["weights_sha256"]), 64)
        self.assert_reaped(session)

    def test_formal_environment_constraints_cross_the_real_worker_and_parent_boundaries(self):
        with DP5qSession() as session:
            request = CheckRequest(compound_id="annotated-acetone", smiles="CC(=O)C",
                carbon_shifts=(207.1, 30.9), carbon_groups=(("C1", "C4"),),
                carbon_labels=("C1", "C2", "C4"), observation_atom_counts=(1, 1))
            result = session.check(request)
            self.assertEqual(result.status, "assessed")
            self.assertEqual({tuple(row["atom_labels"]): row["experimental_shift"] for row in result.assignments},
                             {("C1", "C4"): 30.9, ("C2",): 207.1})
            refused = session.check(CheckRequest(compound_id="annotated-ether", smiles="CCOCC",
                carbon_shifts=(65.9, 15.4), carbon_groups=(("C1", "C5"), ("C2", "C4")),
                observation_atom_counts=(2, None)))
            self.assertEqual(refused.status, "not_applicable")
            self.assertEqual(refused.reason, "annotation_partition_mismatch")
            self.assertIsNone(refused.score_raw)
            self.assertEqual(len(session._processes), 1)
        self.assert_reaped(session)

    def test_unexpected_calculation_error_is_failed_and_next_check_reuses_real_models(self):
        fixture = Path(__file__).parent / "fixtures/dp5q/failing_engine_worker.py"
        with DP5qSession(_command=[sys.executable, "-u", str(fixture), "calculation"]) as session:
            failed = session.check(self.request("fault"))
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.reason, "calculation_failed")
            self.assertIn("controlled unexpected calculation failure", failed.diagnostics["message"])
            self.assertEqual(failed.diagnostics["cause"], "RuntimeError")
            self.assertGreaterEqual(failed.timings["total"], 0)
            for name in ("model_id", "score_version", "preprocessing_id", "geometry_policy", "weights_sha256"):
                self.assertEqual(failed.provenance[name], session.metadata[name])
            self.assertEqual(session.check(self.request("later")).status, "assessed")
            self.assertEqual(len(session._processes), 1)
        self.assert_reaped(session)

    def test_engine_import_failure_is_an_explicit_generation_error(self):
        fixture = Path(__file__).parent / "fixtures/dp5q/failing_engine_worker.py"
        for delay in (0, 1.5):
            with self.subTest(terminal_consumer_delay=delay):
                with DP5qSession(_command=[sys.executable, "-u", str(fixture), "initialization"]) as session:
                    wait = session._wait
                    def delayed_result_wait(kind, deadline):
                        if kind == "result" and delay:
                            time.sleep(delay)  # Let the explicit error and following EOF both arrive.
                        return wait(kind, deadline)
                    session._wait = delayed_result_wait
                    with self.assertRaises(DP5qWorkerError) as raised:
                        session.check(self.request())
                    self.assertEqual(raised.exception.reason, "engine_initialization_failed")
                    self.assertIn("could not initialize", raised.exception.details)
                    self.assertIn("controlled engine initialization failure", raised.exception.stderr_tail)
                self.assert_reaped(session)
                self.assertEqual(session._processes[0].returncode, 0)

    def test_working_directory_modules_are_not_imported_by_doctor_or_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "imported.txt"
            for name in ("numpy", "queue", "json"):
                (root / f"{name}.py").write_text(
                    f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n"
                    "raise RuntimeError('PLANTED-CWD-MODULE')\n", encoding="utf-8")
            # Use the installed editable project, without passing its source on PYTHONPATH.
            with patch("checksi.dp5q.client.Path.cwd", return_value=root), patch.dict(os.environ):
                os.environ.pop("PYTHONPATH", None)
                ready, messages = doctor_runtime()
                self.assertTrue(ready, messages)
                with DP5qSession() as session:
                    self.assertEqual(session.check(self.request()).status, "assessed")
            self.assertFalse(marker.exists())
        self.assert_reaped(session)

    def test_actual_child_refuses_corrupted_weights_without_a_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "checksi"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            shutil.copytree(Path(checksi.dp5q.__file__).parent, package / "dp5q",
                            ignore=shutil.ignore_patterns("__pycache__"))
            weights = package / "dp5q" / "data" / "weights.npz"
            with weights.open("r+b") as stream:
                stream.seek(-1, 2)
                value = stream.read(1)
                stream.seek(-1, 2)
                stream.write(bytes([value[0] ^ 1]))
            with patch.dict(os.environ, {"PYTHONPATH": str(root)}):
                with DP5qSession() as session:
                    with self.assertRaises(DP5qWorkerUnavailable):
                        session.check(self.request())
                    with self.assertRaises(DP5qWorkerUnavailable):
                        session.check(self.request("later"))
            self.assertEqual(len(session._processes), 1)
            self.assert_reaped(session)

    def test_parent_eof_while_idle_closes_the_actual_child(self):
        with DP5qSession() as session:
            session.check(self.request())
            process = session._generation.process
            process.stdin.close()
            self.assertEqual(process.wait(timeout=5), 0)
        self.assert_reaped(session)

    def test_known_inventory_and_early_refusal_keep_one_real_generation(self):
        with DP5qSession() as session:
            refused = session.check(CheckRequest(compound_id="incomplete", smiles="CCO",
                carbon_shifts=(18.5,), carbon_labels=("C1", "C2")))
            self.assertEqual(refused.status, "not_applicable")
            self.assertEqual(refused.reason, "strict_nmr_coverage_mismatch")
            valid = self.request().model_copy(update={"carbon_labels": ("C1", "C2")})
            self.assertEqual(session.check(valid).status, "assessed")
            self.assertEqual(len(session._processes), 1)
        self.assert_reaped(session)

    def test_parent_eof_during_actual_computation_is_observed(self):
        disconnected = []
        def disconnect(event):
            if not disconnected and event.message.endswith(": geometry"):
                disconnected.append(time.monotonic())
                session._generation.process.stdin.close()
        with DP5qSession(event_dispatcher=disconnect) as session:
            with self.assertRaises(DP5qWorkerError):
                session.check(self.long_request())
            self.assertTrue(disconnected, "actual calculation must have emitted progress")
            self.assertEqual(session._processes[0].returncode, 0)
        self.assert_reaped(session)

    def test_parent_eof_bounds_a_blocked_calculation_independently_of_its_return(self):
        fixture = Path(__file__).parent / "fixtures/dp5q/failing_engine_worker.py"
        disconnected = []
        def disconnect(event):
            if not disconnected and event.message.endswith(": geometry"):
                disconnected.append(time.monotonic())
                session._generation.process.stdin.close()
        with DP5qSession(_command=[sys.executable, "-u", str(fixture), "native_block"],
                         settings=SimpleNamespace(timeout_seconds=4), event_dispatcher=disconnect) as session:
            with self.assertRaises(DP5qWorkerError):
                session.check(self.request())
            self.assertTrue(disconnected)
            self.assertLess(time.monotonic() - disconnected[0], 2.5)
            self.assertEqual(session._processes[0].returncode, 0)
        self.assert_reaped(session)

    @unittest.skipUnless(os.name == "posix", "POSIX direct SIGINT during native work")
    def test_direct_child_sigint_during_geometry_remains_parent_owned(self):
        self._assert_direct_child_sigint_is_parent_owned()

    @unittest.skipUnless(os.name == "posix", "POSIX direct SIGINT after native handler replacement")
    def test_direct_child_sigint_after_native_handler_override_is_parent_owned(self):
        fixture = Path(__file__).parent / "fixtures/dp5q/failing_engine_worker.py"
        self._assert_direct_child_sigint_is_parent_owned([sys.executable, "-u", str(fixture), "native_sigint"])

    def _assert_direct_child_sigint_is_parent_owned(self, command=None):
        token = threading.Event()
        observations = []
        def cancel(event):
            if not observations and event.message.endswith(": geometry"):
                observations.append(stack.enter_context(ProcessEscalationObservation(session._generation.process)))
                session._generation.process.send_signal(signal.SIGINT)
                token.set()
        with ExitStack() as stack, DP5qSession(_command=command, event_dispatcher=cancel, cancel_token=token) as session:
            with self.assertRaises(DP5qCancelled):
                session.check(self.long_request())
            self.assertEqual(len(observations), 1)
            observations[0].assert_cancelled_exit(self)
        self.assert_reaped(session)

    @unittest.skipUnless(os.name == "posix", "POSIX direct SIGINT after native handler replacement")
    def test_direct_child_sigint_without_parent_cancellation_keeps_the_generation_healthy(self):
        fixture = Path(__file__).parent / "fixtures/dp5q/failing_engine_worker.py"
        signalled = []
        def interrupt(event):
            if not signalled and event.message.endswith(": geometry"):
                signalled.append(True)
                session._generation.process.send_signal(signal.SIGINT)
        with DP5qSession(_command=[sys.executable, "-u", str(fixture), "native_sigint_complete"],
                         event_dispatcher=interrupt) as session:
            self.assertEqual(session.check(self.request()).status, "assessed")
            self.assertTrue(signalled)
            self.assertEqual(session.check(self.request("later")).status, "assessed")
            self.assertEqual(len(session._processes), 1)
            self.assertEqual(session._failures, 0)
        self.assertEqual(session._processes[0].returncode, 0)
        self.assert_reaped(session)

    def test_cancellation_during_actual_computation_reaps_within_five_seconds(self):
        token = threading.Event()
        cancelled_at = []
        observations = []
        def cancel(event):
            if not cancelled_at and event.message.endswith(": geometry"):
                observations.append(stack.enter_context(ProcessEscalationObservation(session._generation.process)))
                cancelled_at.append(time.monotonic())
                token.set()
        with ExitStack() as stack, DP5qSession(event_dispatcher=cancel, cancel_token=token) as session:
            with self.assertRaises(DP5qCancelled):
                session.check(self.long_request())
            self.assertLess(time.monotonic() - cancelled_at[0], 5)
            with self.assertRaises(DP5qCancelled):
                session.check(self.request("later"))
        self.assertEqual(len(session._processes), 1)
        self.assertEqual(len(observations), 1)
        observations[0].assert_cancelled_exit(self)
        self.assert_reaped(session)


if __name__ == "__main__":
    unittest.main()
