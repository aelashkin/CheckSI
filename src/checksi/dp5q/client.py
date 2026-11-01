"""Parent-side availability and isolated execution of the owned DP5q worker."""

from __future__ import annotations

from copy import deepcopy
import importlib.metadata
import json
import logging
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid

from .assets import AssetError, validate_asset_metadata
from .contracts import (CAPABILITIES, PROTOCOL_VERSION, CheckRequest, CheckResult, Frame, MAX_FRAME_BYTES, decode_frame,
                        encode_frame, validate_result_for_request)

STDERR_BYTES = 64 * 1024
POLL_SECONDS = 0.05
STARTUP_TIMEOUT_SECONDS = 120.0
# Retain exceptional doctor ownership if the OS has not confirmed process exit.
_UNREAPED_DIAGNOSTICS = []
logger = logging.getLogger(__name__)


class _InvalidResult(ValueError):
    """A terminal failed local validation rather than process liveness."""


def _bounded_stderr(value: str) -> str:
    """Keep the decoded tail within the byte cap, including invalid UTF-8."""
    return value.encode("utf-8", errors="replace")[-STDERR_BYTES:].decode("utf-8", errors="ignore").strip()


class DP5qCancelled(RuntimeError):
    """The run was cancelled; this session cannot start another worker."""


class DP5qWorkerError(RuntimeError):
    """One compound failed at the process/protocol boundary."""

    def __init__(self, reason: str, details: str = "", *, stderr_tail: str = "",
                 diagnostic_detail: str = "", cause: str | None = None,
                 returncode: int | None = None, count_crash: bool = True) -> None:
        self.reason = reason
        self.details = details[:1024]
        # Local logs may retain native tracebacks; reports use only the summary.
        self.stderr_tail = _bounded_stderr(stderr_tail)
        self.diagnostic_detail = _bounded_stderr(diagnostic_detail)
        self.cause = cause
        self.returncode = returncode
        self.count_crash = count_crash
        super().__init__(f"{reason}: {self.details}" if self.details else reason)

    def __str__(self) -> str:
        text = f"{self.reason}: {self.details}" if self.details else self.reason
        if self.cause and self.cause != self.reason:
            text += f" (cause: {self.cause})"
        if self.returncode is not None:
            text += f" (worker exit status: {self.returncode})"
        return text


class DP5qWorkerUnavailable(DP5qWorkerError):
    """Startup or the restart budget prevents further checks in this run."""


class _Generation:
    """Own bounded pipes/queues for exactly one process generation."""

    def __init__(self, process, run_id: str, number: int, prefix: str) -> None:
        self.process = process
        self.run_id = run_id
        self.number = number
        self.sequence = 0
        self.responses = queue.Queue(maxsize=2)
        self.outgoing = queue.Queue(maxsize=2)  # one request and one control
        self.failed = threading.Event()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.failure = ("worker_failed", "")
        self.failure_counts_as_crash = True
        self.stderr = bytearray()
        self.progress = None
        self.ready_seen = False
        self.error_seen = False
        self.terminal_sequence = 0
        self.threads = [
            threading.Thread(target=target, name=f"{prefix}-{number}-{name}", daemon=True)
            for name, target in (("stdout", self._read_stdout), ("stderr", self._read_stderr),
                                 ("writer", self._write))
        ]

    def start(self) -> None:
        """Start I/O only after the session has recorded process ownership."""
        for thread in self.threads:
            thread.start()

    def fail(self, reason: str, details: str, *, count_crash: bool = True) -> None:
        """Fatal delivery never blocks a pipe-draining thread."""
        with self.lock:
            if not self.failed.is_set():
                self.failure = (reason, details)
                self.failure_counts_as_crash = count_crash
                self.failed.set()

    def send(self, frame: Frame) -> None:
        raw = encode_frame(frame)
        try:
            self.outgoing.put_nowait(raw)
        except queue.Full:
            self.fail("protocol_error", "Worker command queue overflow.")

    def _write(self) -> None:
        try:
            while not self.stop.is_set():
                try:
                    raw = self.outgoing.get(timeout=POLL_SECONDS)
                except queue.Empty:
                    continue
                view = memoryview(raw)
                while view and not self.stop.is_set():
                    count = self.process.stdin.write(view)
                    if not count:
                        raise OSError("Worker input pipe closed.")
                    view = view[count:]
        except (OSError, ValueError) as exc:
            if not self.stop.is_set():
                self.fail("worker_io_error", str(exc))

    def _read_stdout(self) -> None:
        pending = bytearray()
        try:
            while True:
                chunk = self.process.stdout.read(8192)
                if not chunk:
                    detail = "Worker returned a partial frame." if pending else "Worker closed its output."
                    if pending or not self.error_seen:
                        self.fail("protocol_error" if pending else "worker_exited", detail)
                    return
                pending.extend(chunk)
                responses = []
                while b"\n" in pending:
                    end = pending.index(b"\n") + 1
                    raw = bytes(pending[:end])
                    del pending[:end]
                    frame = self._receive(decode_frame(raw))
                    if frame is not None:
                        responses.append(frame)
                if len(pending) >= MAX_FRAME_BYTES:
                    raise ValueError("Worker returned an oversized frame.")
                # Validate the complete read before publishing any terminal in it.
                for frame in responses:
                    try:
                        self.responses.put_nowait(frame)
                    except queue.Full as exc:
                        raise ValueError("Worker terminal/control queue overflow.") from exc
        except Exception as exc:
            if not self.stop.is_set():
                self.fail("protocol_error", str(exc), count_crash=not isinstance(exc, _InvalidResult))

    def _receive(self, frame: Frame) -> Frame | None:
        if frame.run_id != self.run_id or frame.generation != self.number:
            raise ValueError("Worker returned mismatched run/generation IDs.")
        if frame.kind == "ready":
            if self.ready_seen or frame.sequence != 0:
                raise ValueError("Worker returned duplicate or out-of-order READY.")
            self.ready_seen = True
        elif frame.kind in ("result", "progress"):
            if not self.ready_seen or not self.sequence or frame.sequence != self.sequence:
                raise ValueError("Worker returned a stale or unexpected request ID.")
            if frame.sequence <= self.terminal_sequence:
                raise ValueError("Worker returned duplicate/late compound messages.")
            if frame.kind == "progress":
                stage = frame.payload.get("stage")
                if not isinstance(stage, str) or not 1 <= len(stage) <= 128:
                    raise ValueError("Worker returned invalid progress.")
                with self.lock:
                    self.progress = stage
                return
            try:
                CheckResult.model_validate(frame.payload)
            except Exception as exc:
                raise _InvalidResult(str(exc)) from exc
            self.terminal_sequence = frame.sequence
        elif frame.kind in ("error", "closed"):
            if frame.sequence != self.sequence:
                raise ValueError("Worker returned a mismatched control ID.")
            if frame.kind == "error":
                # Preserve an explicit fatal cause if EOF arrives before the
                # supervisor consumes its reserved terminal delivery.
                self.error_seen = True
        else:
            raise ValueError("Worker returned an unexpected message kind.")
        return frame

    def _read_stderr(self) -> None:
        try:
            while chunk := self.process.stderr.read(8192):
                with self.lock:
                    self.stderr.extend(chunk)
                    del self.stderr[:-STDERR_BYTES]
        except (OSError, ValueError):
            pass

    def error(self) -> DP5qWorkerError:
        with self.lock:
            reason, details = self.failure
        if reason == "worker_failed" and self.process.poll() is not None:
            reason, details = "worker_exited", "Worker exited before the next request."
        return DP5qWorkerError(reason, details, stderr_tail=self.stderr_tail(),
                              returncode=self.process.poll(), count_crash=self.failure_counts_as_crash)

    def stderr_tail(self) -> str:
        """Return capped raw diagnostics for local logs only."""
        with self.lock:
            return _bounded_stderr(self.stderr.decode("utf-8", errors="replace"))


class DP5qSession:
    """Lazy one-worker-per-run supervision; failed compounds are never retried."""

    def __init__(self, settings=None, event_dispatcher=None, cancel_token=None, *,
                 _command=None, startup_timeout=STARTUP_TIMEOUT_SECONDS, cancel_grace=2) -> None:
        self.settings = settings
        self.event_dispatcher = event_dispatcher
        self.cancel_token = cancel_token
        self.startup_timeout = float(startup_timeout)
        self.cancel_grace = float(cancel_grace)
        self.timeout = float(getattr(settings, "timeout_seconds", 600))
        if not all(math.isfinite(value) for value in (self.startup_timeout, self.timeout, self.cancel_grace)) or (
            min(self.startup_timeout, self.timeout) <= 0 or not 0 <= self.cancel_grace <= 2
        ):
            raise ValueError("Worker deadlines must be positive and cancellation grace at most two seconds.")
        self._command = list(_command) if _command is not None else None
        self._run_id = uuid.uuid4().hex
        self._thread_prefix = f"dp5q-{self._run_id}"
        self._generation = None
        self._processes = []
        self._sequence = 0
        self._failures = 0
        self._closed = False
        self._cancelled = False
        self._unavailable = False
        self._metadata = {}

    @property
    def metadata(self) -> dict:
        """Return only validated READY provenance, without starting the child."""
        return deepcopy(self._metadata)

    def __enter__(self) -> DP5qSession:
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def _frame(self, kind: str, payload=None) -> Frame:
        return Frame(kind=kind, run_id=self._run_id, generation=self._generation.number,
                     sequence=self._generation.sequence, payload=payload or {})

    def _is_cancelled(self) -> bool:
        return self._cancelled or bool(self.cancel_token is not None and self.cancel_token.is_set())

    def _cancel(self) -> None:
        self._cancelled = True
        self._stop_generation("cancel")
        raise DP5qCancelled("DP5q calculation cancelled.")

    def _wait(self, kind: str, deadline: float) -> Frame:
        generation = self._generation
        while True:
            if self._is_cancelled():
                self._cancel()
            if generation.failed.is_set():
                raise generation.error()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DP5qWorkerError("worker_timeout", f"Timed out waiting for worker {kind}.", count_crash=False)
            with generation.lock:
                progress, generation.progress = generation.progress, None
            if progress and self.event_dispatcher is not None:
                from checksi.events import ProgressEvent
                self.event_dispatcher(ProgressEvent(0, 0, f"DP5q: {progress}"))
            try:
                frame = generation.responses.get(timeout=min(POLL_SECONDS, remaining))
            except queue.Empty:
                continue
            if self._is_cancelled():
                self._cancel()
            if generation.failed.is_set():
                raise generation.error()
            if time.monotonic() >= deadline:
                raise DP5qWorkerError("worker_timeout", f"Timed out waiting for worker {kind}.", count_crash=False)
            if frame.kind == "error":
                reason = str(frame.payload.get("reason", "worker_failed"))[:128]
                detail = {"model_load_failed": "Worker could not load the bundled model.",
                    "engine_initialization_failed": "Worker could not initialize its calculation engine.",
                    "protocol_error": "Worker rejected the protocol request."}.get(
                        reason, "Worker reported a processing failure.")
                raise DP5qWorkerError(reason, detail, stderr_tail=generation.stderr_tail(),
                                     diagnostic_detail=str(frame.payload.get("details", "")),
                                     count_crash=reason != "protocol_error")
            if frame.kind != kind:
                raise DP5qWorkerError("protocol_error", f"Expected {kind}, received {frame.kind}.")
            return frame

    def _start(self) -> None:
        try:
            expected = check_availability(self.settings)
            number = len(self._processes) + 1
            command = self._command or [sys.executable, "-P", "-u", "-m", "checksi.dp5q.worker"]
            process = subprocess.Popen([*command, "--run-id", self._run_id, "--generation", str(number)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                env=worker_environment(owner_pid=os.getpid()), cwd=str(Path.cwd()), shell=False, **_process_group_options())
            self._processes.append(process)
            self._generation = _Generation(process, self._run_id, number, self._thread_prefix)
            self._generation.start()
            frame = self._wait("ready", time.monotonic() + self.startup_timeout)
            if frame.payload.get('capabilities') != list(CAPABILITIES):
                raise DP5qWorkerError('protocol_error', 'READY lacks the required coincident-observation capability.')
            for name in ("model_id", "score_version", "preprocessing_id", "geometry_policy", "weights_sha256"):
                if frame.payload.get(name) != expected[name]:
                    raise DP5qWorkerError("protocol_error", f"READY {name} does not match installed assets.")
            self._metadata = {**expected, **frame.payload, "run_id": self._run_id, "generation": number}
        except DP5qCancelled:
            raise
        except (DP5qAvailabilityError, DP5qWorkerError, OSError, RuntimeError) as exc:
            self._unavailable = True
            error = exc if isinstance(exc, DP5qWorkerError) else DP5qWorkerError("worker_unavailable", str(exc))
            self._stop_after_error(error)
            raise DP5qWorkerUnavailable("worker_unavailable", error.details, stderr_tail=error.stderr_tail,
                diagnostic_detail=error.diagnostic_detail, cause=error.reason, returncode=error.returncode) from exc

    def check(self, request: CheckRequest) -> CheckResult:
        """Assess one compound; one replacement is reserved for later compounds."""
        try:
            request = CheckRequest.model_validate(request.model_dump(mode="python")
                                                 if isinstance(request, CheckRequest) else request)
        except (ValueError, TypeError, OverflowError) as exc:
            raise DP5qWorkerError("invalid_request", "The compound request is invalid.",
                                 diagnostic_detail=str(exc), count_crash=False) from exc
        if self._is_cancelled():
            self._cancel()
        if self._closed or self._unavailable:
            raise DP5qWorkerUnavailable("worker_unavailable", "DP5q session is closed or unavailable.")
        if self._generation is not None and (
            self._generation.failed.is_set() or self._generation.process.poll() is not None
        ):
            # Nothing has been sent for this compound. Spend the same single
            # replacement budget before assigning it a request sequence.
            generation = self._generation
            error = generation.error()
            self._failures += int(error.count_crash)
            self._stop_after_error(error, wait_for_exit=True)
            logger.warning("Replacing DP5q worker generation %s: %s\n%s\n%s",
                           generation.number, error, error.diagnostic_detail, error.stderr_tail)
            if self._failures >= 2:
                self._unavailable = True
                raise DP5qWorkerUnavailable("worker_unavailable", "Worker replacement budget is exhausted.",
                    stderr_tail=error.stderr_tail, diagnostic_detail=error.diagnostic_detail,
                    cause=error.reason, returncode=error.returncode) from error
        if self._generation is None:
            self._start()
        previous_sequence = self._generation.sequence
        self._sequence += 1
        self._generation.sequence = self._sequence
        try:
            self._generation.send(self._frame("check", request.model_dump(mode="json")))
        except (ValueError, TypeError, OverflowError) as exc:
            self._sequence -= 1
            self._generation.sequence = previous_sequence
            raise DP5qWorkerError("invalid_request", "The compound request could not be encoded.",
                                 diagnostic_detail=str(exc), count_crash=False) from exc
        try:
            deadline = time.monotonic() + self.timeout
            frame = self._wait("result", deadline)
            result = CheckResult.model_validate(frame.payload)
            def validate_cancelled():
                if self._is_cancelled():
                    self._cancel()
                if time.monotonic() >= deadline:
                    raise DP5qWorkerError('request_timeout', 'The request deadline expired during result validation.',
                                         count_crash=False)
            validate_result_for_request(request, result, self._metadata, cancel_check=validate_cancelled)
            return result
        except DP5qCancelled:
            raise
        except (DP5qWorkerError, ValueError) as exc:
            error = exc if isinstance(exc, DP5qWorkerError) else DP5qWorkerError(
                "protocol_error", str(exc), count_crash=False)
            self._failures += int(error.count_crash)
            self._stop_after_error(error, wait_for_exit=error.reason == "worker_exited")
            if self._failures >= 2:
                self._unavailable = True
                raise DP5qWorkerUnavailable("worker_unavailable", error.details, stderr_tail=error.stderr_tail,
                    diagnostic_detail=error.diagnostic_detail, cause=error.reason, returncode=error.returncode) from exc
            if error is exc:
                raise
            raise error from exc

    def _stop_after_error(self, error: DP5qWorkerError, *, wait_for_exit: bool = False) -> None:
        """Preserve the original failure if bounded cleanup also fails."""
        generation = self._generation
        try:
            self._stop_generation(wait_for_exit=wait_for_exit)
        except DP5qWorkerUnavailable as cleanup:
            cleanup.cause = error.reason
            cleanup.diagnostic_detail = _bounded_stderr(str(error) + "\n" + error.diagnostic_detail)
            raise cleanup from error
        finally:
            if generation is not None:
                error.stderr_tail = generation.stderr_tail()
                error.returncode = generation.process.poll()

    def _stop_generation(self, control: str | None = None, *, wait_for_exit: bool = False) -> None:
        generation = self._generation
        if generation is None:
            return
        process = generation.process
        if control and not generation.stop.is_set() and process.poll() is None:
            generation.send(self._frame(control))
            deadline = time.monotonic() + self.cancel_grace
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(POLL_SECONDS)
        elif (wait_for_exit or getattr(generation, "error_seen", False)) and process.poll() is None:
            # EOF can precede process exit while native modules tear down. Give
            # it the same bounded cooperative grace before escalating signals.
            try:
                process.wait(timeout=self.cancel_grace)
            except subprocess.TimeoutExpired:
                pass
        reaped = _terminate_and_reap(process)
        generation.stop.set()
        # Reaping first releases a writer blocked in a full pipe, on all platforms.
        for thread in generation.threads:
            if thread.ident is not None:
                thread.join(timeout=0.25)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
        if not reaped:
            self._unavailable = True
            # Retain ownership so a later close can retry reaping; no restart is
            # permitted while this process's exit remains unconfirmed.
            raise DP5qWorkerUnavailable("worker_cleanup_failed",
                "Worker did not exit after terminate/kill; process exit remains unconfirmed.",
                stderr_tail=generation.stderr_tail())
        self._generation = None

    def close(self) -> None:
        """Cooperatively close, then terminate/kill and reap every owned process."""
        self._closed = True
        if self._generation is not None:
            self._stop_generation("cancel" if self._is_cancelled() else "shutdown")


class DP5qAvailabilityError(RuntimeError):
    """The installed package cannot provide a complete owned DP5q model."""

    def __init__(self, messages: list[str]) -> None:
        self.messages = messages
        super().__init__("\n".join(messages))


def worker_environment(*, owner_pid: int | None = None) -> dict[str, str]:
    """Bound native compute threads before importing numerical libraries."""
    env = os.environ.copy()
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
                 "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
        env[name] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    if owner_pid is not None:
        env["DP5Q_OWNER_PID"] = str(owner_pid)
    return env


def _process_group_options() -> dict:
    """Keep terminal interrupts parent-owned throughout native initialization."""
    return {"start_new_session": True} if os.name == "posix" else {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}


def check_availability(settings=None) -> dict:
    """Check package metadata and assets without importing/loading the models."""
    del settings  # A legacy runtime location is never consulted.
    try:
        metadata = validate_asset_metadata()
        if any(not isinstance(metadata.get(name), str) or not metadata[name] for name in (
            "model_id", "score_id", "preprocessing_id", "geometry_policy"
        )):
            raise AssetError("Model readiness identifiers are missing or invalid.")
        versions = {name: importlib.metadata.version(name) for name in ("numpy", "scipy", "rdkit")}
    except (AssetError, importlib.metadata.PackageNotFoundError) as exc:
        raise DP5qAvailabilityError([
            str(exc),
            "For source-checkout and installed-package recovery, see "
            "https://github.com/aelashkin/CheckSI/blob/main/docs/installation.md#repair-bundled-dp5q-assets",
        ]) from exc
    return {
        "available": True,
        "protocol_version": PROTOCOL_VERSION,
        "capabilities": list(CAPABILITIES),
        "model_id": metadata["model_id"],
        "score_version": metadata["score_id"],
        "preprocessing_id": metadata["preprocessing_id"],
        "geometry_policy": metadata["geometry_policy"],
        "weights_sha256": metadata["files"]["weights.npz"]["sha256"],
        "versions": versions,
    }


def _terminate_and_reap(process) -> bool:
    """Bound escalation and tolerate a process exiting between poll and signal."""
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            return False
    return True


def _run_diagnostics(command: list[str], timeout: float = STARTUP_TIMEOUT_SECONDS) -> tuple[int, str, str]:
    """Capture finite doctor output with bounded buffers and independent drains."""
    for previous in tuple(_UNREAPED_DIAGNOSTICS):
        if _terminate_and_reap(previous):
            _UNREAPED_DIAGNOSTICS.remove(previous)
    if _UNREAPED_DIAGNOSTICS:
        raise DP5qWorkerUnavailable("worker_cleanup_failed", "Previous diagnostic process exit remains unconfirmed.")
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, bufsize=0, env=worker_environment(owner_pid=os.getpid()), cwd=str(Path.cwd()),
        shell=False, **_process_group_options())
    stdout, stderr = bytearray(), bytearray()
    oversized = threading.Event()

    def drain(stream, buffer, *, terminal=False):
        try:
            while chunk := stream.read(8192):
                if terminal:
                    available = max(0, STDERR_BYTES - len(buffer))
                    buffer.extend(chunk[:available])
                    if len(chunk) > available:
                        oversized.set()
                else:
                    buffer.extend(chunk)
                    del buffer[:-STDERR_BYTES]
        except (OSError, ValueError):
            pass

    prefix = f"dp5q-doctor-{uuid.uuid4().hex}"
    threads = [threading.Thread(target=drain, args=(stream, buffer), kwargs={"terminal": terminal},
        name=f"{prefix}-{name}", daemon=True) for name, stream, buffer, terminal in (
            ("stdout", process.stdout, stdout, True), ("stderr", process.stderr, stderr, False))]
    deadline = time.monotonic() + timeout
    try:
        for thread in threads:
            thread.start()
        while process.poll() is None:
            if oversized.is_set():
                raise ValueError("DP5q worker self-check returned oversized diagnostics.")
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, timeout)
            time.sleep(POLL_SECONDS)
    finally:
        reaped = _terminate_and_reap(process)
        if not reaped:
            _UNREAPED_DIAGNOSTICS.append(process)
        for thread in threads:
            if thread.ident is not None:
                thread.join(timeout=0.25)
        process.stdout.close()
        process.stderr.close()
        if not reaped:
            raise DP5qWorkerUnavailable("worker_cleanup_failed", "Diagnostic process exit remains unconfirmed.",
                                       stderr_tail=stderr.decode("utf-8", errors="replace"))
    if oversized.is_set():
        raise ValueError("DP5q worker self-check returned oversized diagnostics.")
    return process.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")


def doctor_runtime(settings=None) -> tuple[bool, list[str]]:
    """Explicitly load/self-check the installed models in an isolated process."""
    try:
        available = check_availability(settings)
        code, stdout, stderr = _run_diagnostics(
            [sys.executable, "-P", "-u", "-m", "checksi.dp5q.worker", "--self-check"])
        def reject_constant(value):
            raise ValueError(f"Nonfinite diagnostic value: {value}")
        payload = json.loads(stdout.strip(), parse_constant=reject_constant)
        json.dumps(payload, allow_nan=False)
        if not isinstance(payload, dict) or type(payload.get("ok")) is not bool or (
            not isinstance(payload.get("messages"), list) or len(payload["messages"]) > 32
            or any(not isinstance(item, str) or len(item) > 4096 for item in payload["messages"])
        ):
            return False, ["DP5q worker self-check returned invalid diagnostics."]
        if code or not payload["ok"]:
            messages = payload["messages"] or [f"Worker exited with status {code}."]
            return False, [f"DP5q worker self-check failed: {message}" for message in messages]
        return True, [f"Owned DP5q ready: {available['model_id']}", *payload["messages"]]
    except DP5qAvailabilityError as exc:
        return False, exc.messages
    except (OSError, subprocess.TimeoutExpired, ValueError, RuntimeError) as exc:
        return False, [f"DP5q worker self-check failed: {exc}"]
