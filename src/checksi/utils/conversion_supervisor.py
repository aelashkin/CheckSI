"""Private owner-pipe supervisor for isolated document conversion.

This file runs under the package interpreter with -I and imports only the standard
library. The caller's non-inheritable control pipe is its lifetime lease. The
supervisor owns staging, the child process tree, its deadline, and failed/orphaned
private files; a successful result stays leased until the caller removes it.
"""
from __future__ import annotations

import errno
import importlib.util
import json
import math
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Callable

POLL_SECONDS = 0.05
PROCESS_REAP_SECONDS = 2.0
MAX_REQUEST_BYTES = 65536
MAX_RESULT_BYTES = 16384


class ConversionCancelled(RuntimeError):
    """The analysis run was cancelled while preparing its highlighting PDF."""


class _ConversionFailed(RuntimeError):
    """An optional conversion failed without invalidating document extraction."""


class _PosixExitObserver:
    """Observe exit while the unreaped launcher reserves its PID and group ID."""

    def __init__(self, pid: int) -> None:
        self._pid = pid
        self._waitid = callable(getattr(os, "waitid", None))
        self._queue = None
        self._exited = False
        if not self._waitid:
            # Python 3.12 macOS has kqueue but does not yet expose waitid.
            self._queue = select.kqueue()
            event = select.kevent(pid, filter=select.KQ_FILTER_PROC,
                                  flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                  fflags=select.KQ_NOTE_EXIT)
            try:
                self._exited = bool(self._queue.control([event], 1, 0))
            except ProcessLookupError:
                # The child may have exited before registration; it is still unreaped.
                self._exited = True
            except BaseException:
                self.close()
                raise

    def exited(self) -> bool:
        """Check exit without consuming the launcher's wait status."""
        if self._waitid:
            return os.waitid(os.P_PID, self._pid,
                             os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        self._exited = self._exited or bool(self._queue.control(None, 1, 0))
        return self._exited

    def close(self) -> None:
        """Release observation resources without reaping the child."""
        if self._queue is not None:
            self._queue.close()
            self._queue = None


def _system_taskkill() -> str:
    """Use the system executable without searching an untrusted manuscript folder."""
    root = Path(os.environ.get("SystemRoot", ""))
    if not root.is_absolute():
        raise OSError("Windows SystemRoot must identify an absolute system directory")
    return str(root / "System32/taskkill.exe")


def _check_cancelled(cancel_token: threading.Event | None) -> None:
    """Give cancellation precedence over an optional conversion result."""
    if cancel_token is not None and cancel_token.is_set():
        raise ConversionCancelled("DOCX-to-PDF conversion cancelled")


def _stop_owned_process(process: subprocess.Popen, log: Callable[[str], None], *, leader_exited: bool = False) -> None:
    """Kill only the process tree started for this private conversion attempt."""
    def kill_launcher() -> None:
        """Do not replace cancellation with a race against the launcher's exit."""
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except OSError as exc:
            log(f"Warning: Could not stop the owned LibreOffice launcher: {exc}")

    try:
        if sys.platform.startswith("win"):
            if hasattr(process, "stop_tree"):
                process.stop_tree()
            else:
                # The PID belongs to this still-live Popen, never to a shared Office instance.
                subprocess.run(
                    [_system_taskkill(), "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=PROCESS_REAP_SECONDS, check=True,
                )
        else:
            # start_new_session makes the owned launcher's PID its process-group ID.
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        # Darwin reports EPERM for a group containing only its unreaped zombie.
        # A live launcher or a different error still needs an actionable warning.
        if not (sys.platform == "darwin" and leader_exited and exc.errno == errno.EPERM):
            log(f"Warning: Could not stop the entire owned LibreOffice process tree: {exc}")
            kill_launcher()
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"Warning: Could not stop the entire owned LibreOffice process tree: {exc}")
        kill_launcher()
    try:
        process.wait(timeout=PROCESS_REAP_SECONDS)
    except subprocess.TimeoutExpired:
        kill_launcher()
        try:
            process.wait(timeout=PROCESS_REAP_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            log("Warning: The owned LibreOffice process did not stop; close that process before retrying.")
    except OSError as exc:
        log(f"Warning: Could not confirm the owned LibreOffice process stopped: {exc}")


def _wait_for_conversion(
    process: subprocess.Popen,
    timeout_seconds: float,
    cancel_token: threading.Event | None,
    log: Callable[[str], None],
) -> None:
    """Keep the POSIX leader unreaped until its entire owned group is stopped."""
    deadline = time.monotonic() + timeout_seconds
    windows = sys.platform.startswith("win")
    observer = None
    group_owned = True
    leader_exited = False
    try:
        if windows:
            def has_exited() -> bool:
                return process.poll() is not None
        else:
            observer = _PosixExitObserver(process.pid)
            has_exited = observer.exited
        while not has_exited():
            _check_cancelled(cancel_token)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ConversionFailed(
                    f"LibreOffice conversion timed out after {timeout_seconds:g} seconds. "
                    "Check the document and LibreOffice installation, or increase conversion.timeout_seconds."
                )
            time.sleep(min(POLL_SECONDS, remaining))
        leader_exited = True
        _check_cancelled(cancel_token)
    except ChildProcessError as exc:
        # An external SIGCHLD handler must not make a recycled numeric PID our target.
        group_owned = False
        raise _ConversionFailed("LibreOffice launcher was reaped outside its conversion owner; "
                                "its process group can no longer be signalled safely.") from exc
    finally:
        if observer is not None:
            observer.close()
        if (not windows and group_owned) or (windows and (hasattr(process, "stop_tree") or process.poll() is None)):
            # On POSIX this also kills lingering descendants after a successful launcher
            # exit. The unreaped leader prevents PID/PGID reuse until wait() below.
            _stop_owned_process(process, log, leader_exited=leader_exited)
    if process.returncode != 0:
        raise _ConversionFailed(
            f"LibreOffice conversion failed with exit status {process.returncode}. "
            "Check that the document opens in LibreOffice and conversion.libreoffice_path is correct."
        )


def _stage_input(request: dict, stopped: threading.Event) -> None:
    """Create the manuscript copy only after owner-loss monitoring is active."""
    attempt = Path(request["attempt"])
    with Path(request["source"]).open("rb") as source, (attempt / "input.docx").open("xb") as target:
        while True:
            _check_cancelled(stopped)
            if time.monotonic() >= request["deadline"]:
                raise _ConversionFailed("LibreOffice conversion timed out while staging the document.")
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            target.write(chunk)
    (attempt / "profile").mkdir(mode=0o700)
    (attempt / "output").mkdir(mode=0o700)


def _launch_converter(command: list[str], attempt: Path, *, _popen=None):
    """Use a private POSIX session or atomic Windows Job Object ownership."""
    if os.name == "nt" and _popen is None:
        # Absolute sibling loading also works from an uninstalled source package.
        spec = importlib.util.spec_from_file_location("_conversion_windows_job", Path(__file__).with_name("windows_job.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.WindowsJobProcess(command, cwd=attempt)
    options = {"creationflags": 0x08000200} if sys.platform.startswith("win") else {"start_new_session": True}
    return (_popen or subprocess.Popen)(command, cwd=attempt, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)


def _watch_owner(descriptor: int, stopped: threading.Event) -> None:
    """EOF and every control message request bounded cancellation."""
    try:
        # Raw reads avoid a daemon thread holding Python's buffered-stdin lock
        # when a normally released successful lease lets the interpreter exit.
        os.read(descriptor, 1)
    finally:
        stopped.set()


def _publish(payload: dict) -> None:
    """Emit one bounded UTF-8 terminal; converter output never enters this pipe."""
    data = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8") + b"\n"
    if len(data) > MAX_RESULT_BYTES:
        data = b'{"status":"failed","message":"Converter diagnostic exceeded its bounded protocol."}\n'
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except (BrokenPipeError, OSError):
        pass


def supervise(request_path: Path) -> int:
    """Own exactly one private attempt until its caller releases the lifetime lease."""
    attempt = request_path.parent
    process = None
    owned = False
    warnings = []
    stopped = threading.Event()
    try:
        if request_path.name != "request.json" or not attempt.name.startswith("docx-pdf-"):
            raise ValueError("Invalid private conversion request location")
        with request_path.open("rb") as stream:
            encoded = stream.read(MAX_REQUEST_BYTES + 1)
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ValueError("Private conversion request exceeds its limit")
        request = json.loads(encoded)
        if Path(request["attempt"]) != attempt or not math.isfinite(request["deadline"]):
            raise ValueError("Invalid private conversion ownership or deadline")
        command = request["command"]
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and "\0" not in item for item in command):
            raise ValueError("Invalid private converter command")
        if attempt.is_symlink() or not attempt.is_dir():
            raise ValueError("Invalid private conversion directory")
        owned = True
        watcher = threading.Thread(target=_watch_owner, args=(sys.stdin.fileno(), stopped), daemon=True)
        watcher.start()
        _stage_input(request, stopped)
        _check_cancelled(stopped)
        remaining = request["deadline"] - time.monotonic()
        if remaining <= 0:
            raise _ConversionFailed("LibreOffice conversion timed out before startup.")
        try:
            process = _launch_converter(command, attempt)
        except OSError as exc:
            raise _ConversionFailed(f"LibreOffice could not start ({exc}). Check conversion.libreoffice_path and executable permissions.") from exc
        try:
            _wait_for_conversion(process, remaining, stopped, warnings.append)
        finally:
            if hasattr(process, "close"):
                process.close()
            process = None
        _publish({"status": "ok", "warnings": warnings})
        # Keep the confidential copy leased across the caller's PDF validation and
        # report export, without holding a Windows working-directory/file handle.
        while attempt.exists() and not stopped.wait(POLL_SECONDS):
            pass
        return 0
    except ConversionCancelled:
        _publish({"status": "cancelled", "warnings": warnings})
        return 0
    except Exception as exc:
        _publish({"status": "failed", "message": str(exc)[:2048], "warnings": warnings})
        return 1
    finally:
        if process is not None:
            try:
                _stop_owned_process(process, warnings.append)
            finally:
                if hasattr(process, "close"):
                    process.close()
        if owned:
            try:
                shutil.rmtree(attempt)
            except FileNotFoundError:
                pass
            except OSError:
                # The caller also attempts cleanup on cooperative failures. Filesystem
                # permission/device failures cannot be overridden safely by this owner.
                pass


if __name__ == "__main__":
    raise SystemExit(supervise(Path(sys.argv[1])))
