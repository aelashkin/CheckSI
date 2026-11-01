"""Isolated, bounded LibreOffice conversion for optional PDF highlighting."""

from __future__ import annotations

import json
import math
import ntpath
import os
import queue
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable

import fitz

from checksi.utils.conversion_supervisor import (
    ConversionCancelled, MAX_REQUEST_BYTES, MAX_RESULT_BYTES, POLL_SECONDS, PROCESS_REAP_SECONDS, _ConversionFailed,
    _PosixExitObserver as _PosixExitObserver,
    _check_cancelled,
    _stop_owned_process as _stop_owned_process,
    _system_taskkill as _system_taskkill,
    _wait_for_conversion as _wait_for_conversion,
)


def _conventional_executables() -> list[Path]:
    """Return platform app locations after command discovery has been attempted."""
    if sys.platform == "darwin":
        roots = [Path("/Applications")]
        try:
            roots.append(Path.home() / "Applications")
        except (OSError, RuntimeError):
            pass
        return [root / "LibreOffice.app/Contents/MacOS/soffice" for root in roots]
    if sys.platform.startswith("win"):
        roots = [Path(value) for key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")
                 if (value := os.environ.get(key)) and Path(value).is_absolute()]
        return [root / "LibreOffice/program" / name
                for root in roots for name in ("soffice.com", "soffice.exe")]
    return []


def _windows_path_lookup(names: tuple[str, ...]) -> Path | None:
    """Search absolute PATH directories without Windows' implicit current folder."""
    current = Path.cwd().resolve()
    for entry in os.get_exec_path():
        directory = Path(entry.strip().strip('"'))
        if not entry or not directory.is_absolute():
            continue
        try:
            directory = directory.resolve(strict=True)
            if directory == current or not directory.is_dir():
                continue
            for name in names:
                candidate = directory / name
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    resolved = candidate.resolve(strict=True)
                    if resolved.parent != current:
                        return resolved
        except OSError:
            continue
    return None


def _configured_windows_executable(configured: str) -> Path | None:
    """Allow deliberate paths; bare configured commands still use trusted PATH."""
    if any(separator in configured for separator in ("/", "\\")) or ntpath.splitdrive(configured)[0]:
        candidate = Path(configured)
        return candidate.resolve() if candidate.is_file() and os.access(candidate, os.X_OK) else None
    names = [configured]
    if not Path(configured).suffix:
        names.extend(configured + suffix for suffix in (".com", ".exe", ".bat", ".cmd"))
    return _windows_path_lookup(tuple(names))


def _find_executable(executable: str | os.PathLike[str] | None) -> Path:
    """Honor a configured executable or find a standard LibreOffice installation."""
    windows = sys.platform.startswith("win")
    if executable is not None:
        configured = os.path.expanduser(os.fspath(executable))
        resolved = (_configured_windows_executable(configured) if windows
                    else shutil.which(configured)) if configured else None
        if resolved:
            return Path(resolved).resolve()
        raise _ConversionFailed(
            f"The configured LibreOffice executable {configured!r} is missing or not executable. "
            "Set conversion.libreoffice_path to a working soffice executable."
        )
    if windows:
        current = Path.cwd().resolve()
        for candidate in _conventional_executables():
            if candidate.is_file() and os.access(candidate, os.X_OK):
                resolved = candidate.resolve()
                if resolved.parent != current:
                    return resolved
        resolved = _windows_path_lookup(("soffice.com", "soffice.exe", "libreoffice.com", "libreoffice.exe"))
        if resolved:
            return resolved
    else:
        for name in ("soffice", "libreoffice"):
            if resolved := shutil.which(name):
                return Path(resolved).resolve()
        for candidate in _conventional_executables():
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate.resolve()
    raise _ConversionFailed(
        "LibreOffice was not found. Install LibreOffice or set conversion.libreoffice_path "
        "to its soffice executable."
    )


def _validate_pdf(path: Path) -> None:
    """Accept only a readable, unencrypted PDF with at least one readable page."""
    if not path.is_file() or path.is_symlink():
        raise _ConversionFailed(
            "LibreOffice did not create a PDF. Check that the DOCX opens in LibreOffice "
            "and that the temporary work directory is writable."
        )
    try:
        with fitz.open(path) as document:
            if not document.is_pdf or document.needs_pass or document.page_count < 1:
                raise ValueError("PDF is encrypted or has no pages")
            document.load_page(0)
    except Exception as exc:
        raise _ConversionFailed(
            "LibreOffice produced an unreadable or empty PDF. Check the document in LibreOffice and retry."
        ) from exc


def _finish_supervisor(process: subprocess.Popen, log: Callable[[str], None] | None = None) -> None:
    """Release its owner lease, wait for bounded cleanup, and close private pipes."""
    try:
        process.stdin.close()
    except (OSError, ValueError):
        pass
    try:
        process.wait(timeout=PROCESS_REAP_SECONDS * 2 + 1)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=PROCESS_REAP_SECONDS)
        except (OSError, subprocess.SubprocessError) as exc:
            if log:
                log(f"Warning: Private conversion cleanup could not be confirmed: {exc}")
    except OSError as exc:
        if log:
            log(f"Warning: Private conversion cleanup could not be confirmed: {exc}")
    finally:
        try:
            process.stdout.close()
        except (OSError, ValueError):
            pass


def _retain_supervisor_lease(process: subprocess.Popen) -> None:
    """Keep ownership until the caller removes its files or this process exits."""
    try:
        process.wait()
    except (OSError, subprocess.SubprocessError):
        pass
    finally:
        for stream in (process.stdin, process.stdout):
            try:
                stream.close()
            except OSError:
                pass


def _run_supervised(command: list[str], source: Path, attempt: Path, timeout_seconds: float,
                    cancel_token: threading.Event | None, log: Callable[[str], None]) -> subprocess.Popen:
    """Receive one bounded terminal while the supervisor independently owns cleanup."""
    request = {"source": str(source), "attempt": str(attempt), "command": command,
               "deadline": time.monotonic() + timeout_seconds}
    encoded = json.dumps(request, ensure_ascii=True, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        raise _ConversionFailed("The configured converter paths exceed the bounded conversion request size.")
    request_path = attempt / "request.json"
    descriptor = os.open(request_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
    helper = Path(__file__).with_name("conversion_supervisor.py")
    options = {"creationflags": 0x08000200} if sys.platform.startswith("win") else {"start_new_session": True}
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-u", str(helper), str(request_path)],
            cwd=helper.parent, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0, close_fds=True, **options,
        )
    except OSError as exc:
        raise _ConversionFailed(f"The private conversion supervisor could not start ({exc}).") from exc
    messages = queue.Queue(maxsize=1)

    def read_terminal() -> None:
        try:
            messages.put(process.stdout.readline(MAX_RESULT_BYTES + 1))
        except (OSError, ValueError):
            messages.put(b"")

    reader = threading.Thread(target=read_terminal, name="document-conversion-result", daemon=True)
    try:
        reader.start()
    except RuntimeError as exc:
        _finish_supervisor(process, log)
        raise _ConversionFailed("The private conversion result reader could not start.") from exc
    try:
        deadline = request["deadline"] + PROCESS_REAP_SECONDS * 2 + 1
        while True:
            _check_cancelled(cancel_token)
            try:
                raw = messages.get(timeout=POLL_SECONDS)
                break
            except queue.Empty:
                if time.monotonic() >= deadline:
                    raise _ConversionFailed("The private conversion supervisor exceeded its cleanup deadline.")
        if len(raw) > MAX_RESULT_BYTES or not raw.endswith(b"\n"):
            raise _ConversionFailed("The private conversion supervisor exited without a valid bounded result.")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise _ConversionFailed("The private conversion supervisor returned an invalid result.") from exc
        if not isinstance(result, dict) or result.get("status") not in ("ok", "failed", "cancelled"):
            raise _ConversionFailed("The private conversion supervisor returned an unknown result.")
        warnings = result.get("warnings", [])
        if not isinstance(warnings, list) or len(warnings) > 16 or not all(isinstance(value, str) for value in warnings):
            raise _ConversionFailed("The private conversion supervisor returned invalid diagnostics.")
        for warning in warnings:
            log(warning)
        _check_cancelled(cancel_token)
        if result["status"] != "ok":
            raise _ConversionFailed(str(result.get("message") or "DOCX conversion stopped before completion."))
        return process
    except BaseException:
        _finish_supervisor(process, log)
        raise
    finally:
        reader.join(timeout=0.1)


def convert_docx_to_pdf(
    input_path: str | os.PathLike[str],
    log: Callable[[str], None] | None = None,
    *,
    work_dir: str | os.PathLike[str],
    executable: str | os.PathLike[str] | None = None,
    timeout_seconds: float = 60,
    cancel_token: threading.Event | None = None,
) -> Path | None:
    """Convert a private source copy and return a validated PDF owned by ``work_dir``.

    The caller owns the existing run-temporary directory and its lifetime. Failed
    attempts are removed here; a successful PDF remains until that owner cleans up.
    A configured executable is authoritative. Cancellation raises ConversionCancelled.
    """
    _log = log or print
    attempt: Path | None = None
    supervisor = None
    succeeded = False
    try:
        _check_cancelled(cancel_token)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise _ConversionFailed("Set conversion.timeout_seconds to a positive finite number.")
        logical_source = Path(os.path.abspath(os.path.normpath(os.fspath(input_path))))
        if logical_source.suffix.lower() != ".docx":
            raise _ConversionFailed("The conversion input must be a readable DOCX file.")
        source = logical_source.resolve(strict=True)
        if not source.is_file():
            raise _ConversionFailed("The conversion input must be a readable DOCX file.")
        root = Path(work_dir).resolve(strict=True)
        if not root.is_dir() or root in (source.parent, logical_source.parent):
            raise _ConversionFailed("Use a separate, existing run-temporary directory for DOCX conversion.")
        converter = _find_executable(executable)
        _check_cancelled(cancel_token)
        attempt = Path(tempfile.mkdtemp(prefix="docx-pdf-", dir=root))
        copied_source = attempt / "input.docx"
        profile = attempt / "profile"
        output_dir = attempt / "output"
        _check_cancelled(cancel_token)
        command = [
            str(converter), f"-env:UserInstallation={profile.as_uri()}",
            "--headless", "--nologo", "--nodefault", "--norestore",
            "--convert-to", "pdf:writer_pdf_Export", "--outdir", str(output_dir), str(copied_source),
        ]
        supervisor = _run_supervised(command, source, attempt, timeout_seconds, cancel_token, _log)
        output_path = output_dir / "input.pdf"
        _validate_pdf(output_path)
        _check_cancelled(cancel_token)
        lease = threading.Thread(target=_retain_supervisor_lease, args=(supervisor,),
                                 name="document-conversion-lease", daemon=True)
        try:
            lease.start()
        except RuntimeError as exc:
            raise _ConversionFailed("The private conversion lifetime lease could not start.") from exc
        _log("DOCX converted to a temporary PDF for highlighting.")
        succeeded = True
        return output_path
    except ConversionCancelled:
        _log("Warning: DOCX conversion cancelled; PDF highlighting was not prepared.")
        raise
    except (_ConversionFailed, OSError, ValueError) as exc:
        _log(f"Warning: PDF highlighting will be skipped: {exc}")
        return None
    finally:
        if supervisor is not None and not succeeded:
            _finish_supervisor(supervisor, _log)
        if attempt is not None and not succeeded:
            try:
                shutil.rmtree(attempt)
            except FileNotFoundError:
                pass
            except OSError as exc:
                _log(f"Warning: Could not remove temporary DOCX conversion files: {exc}")
