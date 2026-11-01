"""One isolated DP5q model load and serial calculation endpoint per run.

Control input is drained during computation. Parent EOF arms a short hard-exit
watchdog independent of the current native stage returning. A live parent owns
bounded cancellation and terminate/kill/reap. This child creates no subprocesses.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import sys
import threading
from time import perf_counter
import traceback

from .contracts import CAPABILITIES, CalculationCancelled, CheckRequest, CheckResult, Frame, MAX_FRAME_BYTES, decode_frame, encode_frame

POLL_SECONDS = 0.05
PARENT_LOSS_GRACE_SECONDS = 1.0


class _OwnerProcess:
    """Observe the explicit CheckSI owner, including Windows launchers."""

    def __init__(self, pid: int, *, _kernel=None) -> None:
        if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
            raise ValueError("A valid DP5q owner PID is required.")
        self.pid = pid
        self.parent_at_start = os.getppid()
        self.kernel, self.handle = None, None
        if os.name == "nt" or _kernel is not None:
            import ctypes
            from ctypes import wintypes
            self.kernel = _kernel or ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            self.kernel.OpenProcess.restype = wintypes.HANDLE
            self.kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            self.kernel.WaitForSingleObject.restype = wintypes.DWORD
            self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            self.kernel.CloseHandle.restype = wintypes.BOOL
            # Synchronization only: never request terminate, VM, or write rights.
            self.handle = self.kernel.OpenProcess(0x00100000, False, pid)

    def is_alive(self) -> bool:
        if self.kernel is not None:
            if not self.handle:
                return False
            result = self.kernel.WaitForSingleObject(self.handle, 0)
            if result in (0, 0x102):
                return result == 0x102
            raise OSError(f"Owner-process wait failed with status {result}.")
        # The POSIX product child has a direct owner. Parent replacement detects
        # death even if a supervisor has not reaped the owner's zombie yet.
        if self.parent_at_start == self.pid and os.getppid() != self.pid:
            return False
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        return True

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class _ParentLossWatchdog:
    """Abandon a process-private calculation after its protocol owner is gone."""

    def __init__(self, owner_pid: int | None = None) -> None:
        self.finished = threading.Event()
        self.armed = threading.Event()
        self.thread = None
        self.owner = _OwnerProcess(owner_pid) if owner_pid is not None else None
        self.monitor_thread = None

    def start(self) -> None:
        """Watch owner death before model imports without touching input pipes."""
        if self.owner is not None:
            self.monitor_thread = threading.Thread(target=self._monitor, name="dp5q-owner", daemon=True)
            try:
                self.monitor_thread.start()
            except RuntimeError:
                self.owner.close()
                raise

    def _monitor(self) -> None:
        try:
            while not self.finished.wait(POLL_SECONDS):
                try:
                    alive = self.owner.is_alive()
                except OSError:
                    alive = False
                if not alive:
                    self.arm()
                    return
        finally:
            # Close in the waiting thread, never concurrently with a Win32 wait.
            self.owner.close()

    def arm(self) -> None:
        if self.finished.is_set() or self.armed.is_set():
            return
        self.armed.set()
        self.thread = threading.Thread(target=self._wait, name="dp5q-parent-loss", daemon=True)
        try:
            self.thread.start()
        except RuntimeError:
            os._exit(0)

    def _wait(self) -> None:
        if not self.finished.wait(PARENT_LOSS_GRACE_SECONDS):
            # The owner disconnected, so no terminal can be delivered. Engine
            # computation has no file side effects; bypassing native teardown is
            # the bounded fallback when it has not cooperated with cancellation.
            os._exit(0)

    def close(self) -> None:
        self.finished.set()
        if self.monitor_thread is not None and self.monitor_thread.ident is not None:
            self.monitor_thread.join(timeout=.25)
        if self.thread is not None and self.thread.ident is not None:
            self.thread.join(timeout=.1)


class _Output:
    """Reserve bounded terminal delivery while coalescing expendable progress."""

    def __init__(self, stream, run_id: str, generation: int, stopping: threading.Event,
                 on_parent_loss=None) -> None:
        self.stream = stream
        self.run_id = run_id
        self.generation = generation
        self.stopping = stopping
        self.on_parent_loss = on_parent_loss
        self.terminals = queue.Queue(maxsize=2)
        self.lock = threading.Lock()
        self.latest_progress = None
        self.finishing = threading.Event()
        self.thread = threading.Thread(target=self._write, name="dp5q-output", daemon=True)
        self.thread.start()

    def send(self, kind: str, sequence: int, payload=None) -> threading.Event:
        sent = threading.Event()
        try:
            frame = Frame(kind=kind, run_id=self.run_id, generation=self.generation,
                          sequence=sequence, payload=payload or {})
            self.terminals.put_nowait((encode_frame(frame), sent))
        except (queue.Full, ValueError):
            self.stopping.set()
        return sent

    def progress(self, sequence: int, stage: str) -> None:
        if self.stopping.is_set():
            raise CalculationCancelled()
        if not isinstance(stage, str) or not 1 <= len(stage) <= 128:
            raise ValueError("Invalid calculation progress stage.")
        raw = encode_frame(Frame(kind="progress", run_id=self.run_id, generation=self.generation,
                                 sequence=sequence, payload={"stage": stage}))
        with self.lock:
            self.latest_progress = raw

    def _write(self) -> None:
        try:
            while not self.finishing.is_set() or not self.terminals.empty():
                try:
                    raw, sent = self.terminals.get(timeout=POLL_SECONDS)
                    # A terminal supersedes progress not yet written for its request.
                    with self.lock:
                        self.latest_progress = None
                except queue.Empty:
                    with self.lock:
                        raw, self.latest_progress = self.latest_progress, None
                    sent = None
                if raw:
                    view = memoryview(raw)
                    while view:
                        count = self.stream.write(view)
                        if not count:
                            raise OSError("Protocol output closed.")
                        view = view[count:]
                    self.stream.flush()
                    if sent is not None:
                        sent.set()
        except (OSError, ValueError):
            self.stopping.set()
            if self.on_parent_loss is not None:
                self.on_parent_loss()

    def finish(self) -> None:
        self.finishing.set()
        self.thread.join(timeout=0.5)


class _CommandReader:
    """Read one outstanding command plus controls without blocking calculation."""

    def __init__(self, stream, run_id: str, generation: int, output, on_parent_loss=None) -> None:
        self.stream = stream
        self.run_id = run_id
        self.generation = generation
        self.output = output
        self.on_parent_loss = on_parent_loss
        self.commands = queue.Queue(maxsize=1)
        self.cancel = threading.Event()
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.ready = False
        self.busy = False
        self.sequence = 0
        self.failed = False
        self.pending = bytearray()
        self.thread = threading.Thread(target=self.read, name="dp5q-input", daemon=True)

    def _readline(self) -> bytes:
        """Frame raw pipe chunks without a buffered-reader finalization lock."""
        while b"\n" not in self.pending and len(self.pending) <= MAX_FRAME_BYTES:
            chunk = self.stream.read(min(8192, MAX_FRAME_BYTES + 1 - len(self.pending)))
            if not chunk:
                raw = bytes(self.pending)
                self.pending.clear()
                return raw
            self.pending.extend(chunk)
        end = self.pending.find(b"\n")
        end = end + 1 if end >= 0 else len(self.pending)
        raw = bytes(self.pending[:end])
        del self.pending[:end]
        return raw

    def fail(self, details: str) -> None:
        with self.lock:
            if not self.failed:
                self.failed = True
                self.output.send("error", self.sequence, {"reason": "protocol_error", "details": details[:4096]})
        self.cancel.set()
        self.stopping.set()

    def read(self) -> None:
        try:
            while not self.stopping.is_set():
                raw = self._readline()
                if not raw:
                    self.cancel.set()
                    self.stopping.set()
                    if self.on_parent_loss is not None:
                        self.on_parent_loss()
                    return
                frame = decode_frame(raw)
                if frame.run_id != self.run_id or frame.generation != self.generation:
                    raise ValueError("Mismatched run/generation IDs.")
                if frame.kind in ("cancel", "shutdown"):
                    if frame.sequence != self.sequence or frame.payload:
                        raise ValueError("Invalid control request.")
                    self.cancel.set()
                    self.stopping.set()
                    self.output.send("closed", frame.sequence)
                    return
                if frame.kind != "check" or not self.ready or frame.sequence <= self.sequence:
                    raise ValueError("Unexpected, stale or pre-READY request.")
                with self.lock:
                    if self.busy:
                        raise ValueError("Only one compound may be outstanding.")
                    self.sequence = frame.sequence
                request = CheckRequest.model_validate(frame.payload)
                with self.lock:
                    self.busy = True
                try:
                    self.commands.put_nowait((frame, request))
                except queue.Full as exc:
                    raise ValueError("Worker request queue overflow.") from exc
        except Exception as exc:
            self.fail(str(exc))

    def complete(self) -> None:
        with self.lock:
            self.busy = False


def _ready_metadata(models) -> dict:
    metadata = models.metadata
    return {"model_id": metadata["model_id"], "score_version": metadata["score_id"],
            "capabilities": list(CAPABILITIES),
            "weights_sha256": metadata["files"]["weights.npz"]["sha256"],
            "preprocessing_id": metadata["preprocessing_id"],
            "geometry_policy": metadata["geometry_policy"]}


def _serve(input_stream, output_stream, run_id: str, generation: int, on_parent_loss=None) -> int:
    """Load models once, then serve validated serial requests until EOF/control."""
    stopping = threading.Event()
    output = _Output(output_stream, run_id, generation, stopping, on_parent_loss)
    reader = _CommandReader(input_stream, run_id, generation, output, on_parent_loss)
    # Writer failure and input EOF share the same computation stop signal.
    reader.stopping = stopping
    reader.cancel = stopping
    try:
        # Windows native DLL initialization can inspect stdin; a concurrent
        # blocking pipe read deadlocks it (numpy/numpy#24290). Initialize both
        # NumPy and SciPy before the control reader can block on that pipe.
        from .network import load_models
        models = load_models()
        check_compound = None
        engine_error = None
        try:
            from .engine import check_compound
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            engine_error = str(exc)[:4096]
        reader.thread.start()
        if stopping.is_set():
            return 0
        reader.ready = True
        ready = _ready_metadata(models)
        output.send("ready", 0, ready)
        first_check = True
        while not stopping.is_set():
            try:
                frame, request = reader.commands.get(timeout=POLL_SECONDS)
            except queue.Empty:
                continue
            started = perf_counter()
            try:
                if first_check:
                    first_check = False
                    output.progress(frame.sequence, "initialization")
                    if engine_error is not None:
                        output.send("error", frame.sequence, {"reason": "engine_initialization_failed", "details": engine_error})
                        stopping.set()
                        break
                result = check_compound(request, models, cancel_event=reader.cancel,
                    progress=lambda stage: output.progress(frame.sequence, stage))
                result = CheckResult.model_validate(result)
                if result.compound_id != request.compound_id:
                    raise ValueError("Calculation returned a mismatched compound ID.")
            except (CalculationCancelled, KeyboardInterrupt):
                stopping.set()
                break
            except Exception as exc:
                traceback.print_exc(file=sys.stderr)
                result = CheckResult(compound_id=request.compound_id, status="failed",
                    reason="calculation_failed", provenance=ready,
                    timings={"total": perf_counter() - started},
                    diagnostics={"message": str(exc)[:4096] or type(exc).__name__, "cause": type(exc).__name__})
            reader.complete()
            if not stopping.is_set():
                sent = output.send("result", frame.sequence, result.model_dump(mode="python"))
                while not stopping.is_set() and not sent.wait(POLL_SECONDS):
                    pass
        return 0 if not reader.failed else 2
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        output.send("error", reader.sequence, {"reason": "model_load_failed", "details": str(exc)[:4096]})
        return 2
    finally:
        stopping.set()
        reader.cancel.set()
        output.finish()


def _self_check(output_stream) -> int:
    """Perform one actual finite prediction in the short-lived doctor process."""
    try:
        from rdkit import Chem
        from rdkit.Chem import rdDistGeom
        from .network import load_models
        from .engine import check_compound
        from .contracts import validate_result_for_request
        models = load_models()
        mol = Chem.AddHs(Chem.MolFromSmiles("CCO"))
        if rdDistGeom.EmbedMolecule(mol, randomSeed=0xD05) != 0:
            raise ValueError("Diagnostic geometry failed.")
        prediction = models.predict(mol)
        import numpy as np
        if prediction.mean.shape != (2,) or prediction.quantiles.shape != (2, 99) or not (
            np.isfinite(prediction.mean).all() and np.isfinite(prediction.quantiles).all()
        ):
            raise ValueError("Diagnostic predictions are invalid.")
        request = CheckRequest(compound_id="doctor-ethanol", smiles="CCO", carbon_shifts=(18.5, 58.))
        result = check_compound(request, models)
        validate_result_for_request(request, result, _ready_metadata(models))
        if result.status != "assessed":
            raise ValueError(result.diagnostics.get("message") or "Diagnostic engine assessment was unavailable.")
        payload = {"ok": True, "messages": ["Verified bundled weights, finite mean/quantile predictions and a complete engine assessment."]}
        code = 0
    except Exception as exc:
        payload = {"ok": False, "messages": [str(exc)[:4096]]}
        code = 2
    output_stream.write((json.dumps(payload, allow_nan=False, separators=(",", ":")) + "\n").encode())
    output_stream.flush()
    return code


def main(argv=None) -> int:
    """Reserve stdout for the protocol before importing any native libraries."""
    # The parent owns terminal interruption and sends cancellation over stdin.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if os.name == "posix":
        # RDKit temporarily replaces SIGINT handlers. Blocking it before native
        # imports also covers direct child signals; all child threads inherit it.
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id")
    parser.add_argument("--generation", type=int)
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args(argv)
    if not args.self_check and (not args.run_id or len(args.run_id) > 128 or not args.generation or args.generation < 1):
        parser.error("A valid --run-id and --generation are required.")
    from .client import worker_environment
    os.environ.update(worker_environment())
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    # Both daemon I/O threads use unbuffered descriptors: interpreter shutdown
    # never waits for the BufferedReader lock held by a pending stdin read.
    # Process exit closes descriptors, including a blocked reader or writer.
    watchdog = _ParentLossWatchdog(int(os.environ.get("DP5Q_OWNER_PID", os.getppid())))
    watchdog.start()
    try:
        if args.self_check:
            return _self_check(protocol)
        input_stream = os.fdopen(os.dup(sys.stdin.fileno()), "rb", buffering=0)
        return _serve(input_stream, protocol, args.run_id, args.generation, watchdog.arm)
    finally:
        watchdog.close()


if __name__ == "__main__":
    raise SystemExit(main())
