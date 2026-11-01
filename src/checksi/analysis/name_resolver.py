"""Bounded name resolution with explicit accepted-name provenance."""

from __future__ import annotations

import json
import math
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from typing import Any
import warnings

import regex

from checksi.config_schema import AppConfig, DeepNameAnalysis
from checksi.errors import ErrorCode
from checksi.errors.manager import emit_finding
from checksi.errors.findings import FindingSink, messages
from checksi.utils.error_handler import ErrorTracker
from checksi.utils.name_boundaries import (
    FORBIDDEN_IDENTIFIER_UNITS,
    IDENTIFIER_PATTERNS as _PATTERNS,
    balanced_identifier_starts,
    identifier_allowed,
    unproven_bracket_fragment,
)

_FORBIDDEN_UNITS = FORBIDDEN_IDENTIFIER_UNITS


_TIMEOUT_SECONDS = 30.0
_MAX_ATTEMPTS = 8
MAX_NAME_BYTES = 16_384
MAX_PARAGRAPH_BYTES = 65_536
MAX_SMILES_BYTES = 65_536
MAX_MESSAGE_BYTES = 131_072
POLL_SECONDS = 0.05

_PROSE_PREFIX = regex.compile(r"^(?:synthesis|preparation|characterization|characterisation) of\s+", regex.I)
# Standard name boundaries: a sentence end, a colon not after a locant (extraction
# spaces every colon, as in "2,2': 6'" or "[1,2-b: 4,5-b']"), a footnote after a
# lowercase prose word, a heading's standalone compound label ("azirine 3c "; locants
# such as "4a,5" are attached), or a CheckSI label in an annotated report.
# Semicolons stay inside salt names. One whitespace keeps reverse scans linear.
_TOOL_LABELS = r"\bcomment id \d+|\bparsed name:|\bPDF error checker"
_TOOL_LABEL = r"(?:" + _TOOL_LABELS + r")(?:\s|$)"  # A trailing label leaves no name.
_BOUNDARY = (r"(?:(?:[.!?]|(?<![\d'’′″‴]|[-,\[]\p{L}):|(?<=\b(?-i:\p{Ll}{3,}))\.\d{1,3}|"
             r"(?<!\S)(?-i:\d{1,3}[a-z]{1,2}))\s|"
             + _TOOL_LABEL + ")")
_LAST_BOUNDARY = regex.compile(r"(?r)" + _BOUNDARY, regex.I)
_LAST_TOOL_LABEL = regex.compile(r"(?r)" + _TOOL_LABEL, regex.I)
_PAGE_LABEL = r"(?-i:S\d+)"
_PAGE_LABELS = regex.compile(rf"(?:{_PAGE_LABEL}\s+)+")
_PROPERTY_CUE = (
    r"\b(?:(?:colou?rless|(?:pale\s+)?yellow)\s+(?:oil|solid|crystals?)|yield)\b|"
    r"\broute(?:(?:\s*[:=]\s*|\s+)(?:[A-Z]\d*|\d+[A-Z]?)|(?-i:[A-Z])\d*|\d+)\b"
)
_PROPERTY_COLON = regex.compile(r"(?:" + _PROPERTY_CUE + r"|\broute)\s*:\s*$", regex.I)
_HEADING_FOLLOWUP_TEXT = r"product\s+\S+\s+was\s+(?:synthesized|prepared)\b|" + _PROPERTY_CUE
_HEADING_FOLLOWUP_CUE = regex.compile(_HEADING_FOLLOWUP_TEXT, regex.I)
_HEADING_FOLLOWUP = regex.compile(r"^\s*[.;:]?\s*(?:" + _HEADING_FOLLOWUP_TEXT + r")", regex.I)
_PROPERTY_IDENTIFIER = regex.compile(r'%|\byield\b|^(?:GP|ref\.?)[ -]?\d+$', regex.I)
_LAST_PROPERTY_CUE = regex.compile(r'(?r)(?:' + _PROPERTY_CUE + r')', regex.I)
_PROPERTY_LABEL_TAIL = regex.compile(
    r'\s*(?:[,;:]\s*)?(?:(?:sample|route|ref\.?|table)\s*[:=]?\s*[A-Za-z0-9.-]+'
    r'|\d+(?:\.\d+)?\s*%)?\s*', regex.I,
)
# Generic non-name context. Boundaries and page labels are isolated; any other
# context refuses that extraction rather than guessing a chemical suffix.
_MIXED_CONTEXT = regex.compile(
    _PROPERTY_CUE + "|" +
    r"\b(?:NMR|HRMS|MHz|calcd|found|supporting information|experimental|university|melting point|"
    r"optical rotation|specific rotation|manuscript|unpublished|private|confidential|procedure|protocol|"
    r"reaction|mixture|substrate|was|were|stirred|dissolved|concentrated|purified|afforded|yielded|washed|"
    r"heated|cooled|filtered|extracted|evaporated|added|dried|isolated|obtained|gave|produced|overnight)\b|"
    r"(?-i:δ)\s*[=:]?\s*\d|[⚠❗]|\S@\S|https?://|\bIR\s*(?::|\((?:KBr|neat|film|ATR)\))|\bcm\s*[-−–]\s*1|"
    r"^Amides\s|\(KBr\)|\[\s*M\s*[+−–—\-\]]|^\d+\s+|"
    r"\b(?:m\.\s*p\.?|mp)\s*[:=]?\s*\d|\[\s*α\s*\]|^(?:" + _PAGE_LABEL + r"|a|an|the|this|these|those|we|our|after|following|upon)\s|"
    r"\b\d+(?:\.\d+)?\s*(?:mL|[µμu]L|mg|kg|mmol|[µμu]mol|mol|equiv)\b|"
    r"\b\d+(?:\.\d+)?\s+(?:g|L|h|hr|hours?|mins?|minutes?)\b|\b\d+(?:\.\d+)?\s*°\s*[CF]\b|" + _BOUNDARY,
    regex.I,
)


class _ResolutionStopped(Exception):
    def __init__(self, status: str, reason: str):
        self.status = status
        self.reason = reason


class _Budget:
    def __init__(self, timeout: float, cancel_token: threading.Event | None):
        self.deadline = time.monotonic() + timeout
        self.cancel_token = cancel_token

    def check(self) -> float:
        if self.cancel_token is not None and self.cancel_token.is_set():
            raise _ResolutionStopped("cancelled", "cancel_requested")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _ResolutionStopped("budget_exhausted", "deadline")
        return remaining


def _local_smiles(name: str, state: dict) -> str | None:
    if state.get("unavailable"):
        raise RuntimeError("Local OPSIN initialization remains unavailable.")
    if "opsin" not in state:
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message="pkg_resources is deprecated as an API.*", category=UserWarning)
                from pyopsin import PyOpsin
            state["opsin"] = PyOpsin()
        except Exception:
            state["unavailable"] = True
            raise
    result = state["opsin"].to_smiles(name)
    return (result[0] if result else None) if isinstance(result, list) else result


def _valid_smiles(value: Any) -> bool:
    return (isinstance(value, str) and bool(value.strip())
            and len(value.encode("utf-8")) <= MAX_SMILES_BYTES)


def _write_message(stream, payload: dict) -> None:
    encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8") + b"\n"
    if len(encoded) > MAX_MESSAGE_BYTES:
        raise ValueError("Resolver protocol message exceeds the byte limit.")
    stream.write(encoded)
    stream.flush()


def _read_message(stream) -> dict:
    encoded = stream.readline(MAX_MESSAGE_BYTES + 1)
    if not encoded:
        raise EOFError
    if len(encoded) > MAX_MESSAGE_BYTES or not encoded.endswith(b"\n"):
        raise ValueError("Invalid resolver protocol message.")
    payload = json.loads(encoded)
    if not isinstance(payload, dict):
        raise ValueError("Resolver protocol message must be an object.")
    return payload


def _deny_helper_creation(event: str, args: tuple) -> None:
    if event in {"subprocess.Popen", "os.fork", "os.forkpty", "os.posix_spawn", "os.exec", "os.spawn", "os.system"}:
        raise RuntimeError("Windows resolver discovery cannot create helper processes.")


def _worker_main() -> None:
    """Serve one run without importing or reexecuting the caller's main script."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if os.name == "nt":
        sys.addaudithook(_deny_helper_creation)
    # Keep a private protocol descriptor; Python and native JVM stdout become diagnostics.
    output = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        import msvcrt

        # The JVM reads Win32 standard handles, independently of CRT fd1.
        set_stdout = ctypes.WinDLL("kernel32", use_last_error=True).SetStdHandle
        set_stdout.argtypes = (wintypes.DWORD, wintypes.HANDLE)
        set_stdout.restype = wintypes.BOOL
        if not set_stdout(wintypes.DWORD(-11), msvcrt.get_osfhandle(sys.stderr.fileno())):
            raise ctypes.WinError(ctypes.get_last_error())
    state: dict = {}
    try:
        with output:
            while True:
                command = _read_message(sys.stdin.buffer)
                name, backend, timeout = command["name"], command["backend"], command["timeout"]
                if (not isinstance(name, str) or len(name.encode("utf-8")) > MAX_NAME_BYTES
                        or backend != "local"
                        or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                        or not math.isfinite(timeout) or timeout <= 0):
                    return
                try:
                    smiles = _local_smiles(name, state)
                    result = {"status": "resolved" if smiles is not None else "unresolved", "smiles": smiles}
                except Exception:
                    status = "local_unavailable" if state.get("unavailable") else "unavailable"
                    result = {"status": status, "smiles": None}
                try:
                    _write_message(output, result)
                except ValueError:
                    _write_message(output, {"status": "invalid_response", "smiles": None})
    except (EOFError, OSError, ValueError, KeyError, TypeError):
        pass


class _ResolverWorker:
    def __init__(self):
        self.process: subprocess.Popen | None = None
        self.io_thread: threading.Thread | None = None

    def request(self, backend: str, name: str, budget: _Budget) -> dict:
        budget.check()
        if self.process is None:
            options = ({"start_new_session": True} if os.name == "posix" else
                       {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP})
            self.process = subprocess.Popen(
                [sys.executable, "-I", "-u", "-m", "checksi.analysis.name_resolver"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                close_fds=True, **options,
            )
        responses = queue.Queue(maxsize=1)

        def exchange():
            try:
                _write_message(self.process.stdin, {"backend": backend, "name": name, "timeout": budget.check()})
                responses.put((_read_message(self.process.stdout), None))
            except Exception as exc:
                responses.put((None, exc))

        # Pipe writes and partial-line reads must not block the deadline watcher.
        self.io_thread = threading.Thread(target=exchange, name="resolver-protocol", daemon=True)
        self.io_thread.start()
        while True:
            remaining = budget.check()
            try:
                result, error = responses.get(timeout=min(POLL_SECONDS, remaining))
            except queue.Empty:
                continue
            self.io_thread.join()
            budget.check()  # Cancellation wins over a just-arrived success.
            if error is not None:
                raise error
            if (result.get("status") not in {"resolved", "unresolved", "unavailable", "local_unavailable", "invalid_response"}
                    or (result["status"] != "resolved" and result.get("smiles") is not None)):
                raise ValueError("Invalid resolver backend result.")
            if result["status"] == "resolved" and not _valid_smiles(result.get("smiles")):
                return {"status": "invalid_response", "smiles": None}
            return result

    def close(self) -> bool:
        """Kill and reap only this worker and its private discovery helpers."""
        reaped = True
        if self.process is not None:
            try:
                # Do not reap the leader before killing its group: discovery helpers
                # can outlive it, and the unreaped PID preserves ownership identity.
                if os.name == "posix":
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        # Darwin may reject a group containing only its zombie leader.
                        if sys.platform != "darwin":
                            raise
                        self.process.wait(timeout=0.25)
                else:
                    # Inspected Windows discovery is in-process; the child guard
                    # disallows Python helper creation before importing PyOpsin.
                    self.process.kill()
                self.process.wait(timeout=1.0)
            except (OSError, subprocess.SubprocessError):
                reaped = False
                try:
                    self.process.kill()
                    self.process.wait(timeout=1.0)
                except (OSError, subprocess.SubprocessError):
                    pass
        if self.io_thread is not None:
            self.io_thread.join(timeout=0.25)
            reaped = reaped and not self.io_thread.is_alive()
        if (self.process is not None and self.process.returncode is not None
                and (self.io_thread is None or not self.io_thread.is_alive())):
            for stream in (self.process.stdin, self.process.stdout):
                try:
                    stream.close()
                except OSError:
                    pass
            self.process = None
        return reaped


class _ResolverSession:
    """Run-owned worker and local-availability cache; never shared globally."""
    def __init__(self, log=None):
        self.worker = None
        self.local_unavailable = False
        self.cleanup_failed = False
        self.log = log

    def resolve(self, name: str, budget: _Budget) -> str | None:
        if self.cleanup_failed:
            raise RuntimeError("A previous resolver worker could not be reaped.")
        if self.local_unavailable:
            return None
        if self.worker is None:
            self.worker = _ResolverWorker()
        result = self.worker.request("local", name, budget)
        if result["status"] == "local_unavailable":
            self.local_unavailable = True
            if self.log:
                self.log("Local OPSIN unavailable; check JAVA_HOME for a working Java installation. "
                         "Name resolution could not be performed.")
        if result["status"] == "resolved":
            return result["smiles"]
        return None

    def close(self) -> bool:
        reaped = self.worker is None or self.worker.close()
        if reaped:
            self.worker = None
        elif not self.cleanup_failed and self.log:
            self.log("Warning: Name resolver worker cleanup could not be confirmed.")
        self.cleanup_failed = self.cleanup_failed or not reaped
        return reaped


def _name_candidates(original: str, budget: _Budget):
    """Yield bounded mechanical/prose variants before single-hyphen variants."""
    yield original, []
    name, changes = original, []
    for kind, transform in (
        ("normalize_whitespace", lambda value: " ".join(value.split())),
        ("normalize_dashes", lambda value: value.replace("–", "-").replace("—", "-")),
        ("join_hyphen_whitespace", lambda value: value.replace("- ", "-")),  # whitespace is normalized
        ("remove_terminal_period", lambda value: value[:-1] if value.endswith(".") else value),
    ):
        updated = transform(name)
        if updated != name:
            changes = changes + [{"kind": kind}]
            name = updated
    if changes:
        yield name, changes
    prefix = _bounded(_PROSE_PREFIX.match, name, budget)
    if prefix:
        name = name[prefix.end():]
        changes = changes + [{"kind": "remove_recognized_prose"}]
        yield name, changes
    # At most eight speculative candidates can ever be dispatched.
    for pos in [index for index, char in enumerate(name) if char == "-"][-_MAX_ATTEMPTS:][::-1]:
        yield name[:pos] + name[pos + 1:], changes + [{"kind": "remove_single_hyphen"}]


def _bounded(method, text: str, budget: _Budget):
    """Run one heuristic regex within the deadline; runaway work refuses extraction."""
    try:
        return method(text, concurrent=True, timeout=min(POLL_SECONDS, budget.check()))
    except TimeoutError:
        budget.check()
        raise _ResolutionStopped("refused", "extraction_work_limit") from None


def _isolated_name(prefix: str, budget: _Budget):
    """Return a clean prefix, or its last standard segment without page labels, else None.

    Text over the name byte limit is not searched; the caller refuses its size.
    """
    def fits(text):
        return len(text.encode("utf-8")) <= MAX_NAME_BYTES

    if fits(prefix) and not unproven_bracket_fragment(prefix) and not _bounded(_MIXED_CONTEXT.search, prefix, budget):
        return prefix, []
    # Report findings (⚠/❗) make only text after a CheckSI label a name.
    report = "⚠" in prefix or "❗" in prefix
    boundary = _bounded((_LAST_TOOL_LABEL if report else _LAST_BOUNDARY).search, prefix, budget)
    if report and not boundary:
        return None
    # A yield value or route label after a property colon is still prose.
    if boundary and _bounded(_PROPERTY_COLON.search, prefix[:boundary.end()], budget):
        return None
    segment = prefix[boundary.end():].lstrip() if boundary else prefix
    labels = _bounded(_PAGE_LABELS.match, segment, budget)
    segment = segment[labels.end():] if labels else segment
    # Unbalanced brackets mean the boundary fell inside a name.
    if (not segment or unproven_bracket_fragment(segment)
            or any(segment.count(opening) != segment.count(closing) for opening, closing in ("()", "[]", "{}"))
            or (fits(segment) and _bounded(_MIXED_CONTEXT.search, segment, budget))):
        return None
    return segment, [{"kind": "isolate_name_segment"}] if segment != prefix else []


def _extracted_names(paragraph: str, budget: _Budget):
    seen = set()
    balanced_starts = None
    has_followup = _bounded(_HEADING_FOLLOWUP_CUE.search, paragraph, budget) is not None
    for pattern, stat_key in _PATTERNS:
        if stat_key == "single_uppercase_id_match_count":
            balanced_starts = balanced_identifier_starts(paragraph)
        valid_match = None
        heading_match = None
        try:
            for match in regex.finditer(pattern, paragraph, flags=regex.VERSION0,
                                        concurrent=True, timeout=min(POLL_SECONDS, budget.check())):
                budget.check()
                if identifier_allowed(paragraph, match.start(), match.end(), match.group(1), stat_key,
                                      prefix_balanced=match.start() in balanced_starts if balanced_starts is not None else None):
                    valid_match = match
                    # A complete heading before an explicit preparation/property
                    # paragraph owns the compound, rather than a later yield ID.
                    followed_by_prose = has_followup and _bounded(
                        _HEADING_FOLLOWUP.match, paragraph[match.end():match.end() + 256], budget
                    )
                    prefix = paragraph[:match.start()].strip() if followed_by_prose or heading_match is not None else ''
                    isolated = _isolated_name(prefix, budget) if prefix else None
                    if followed_by_prose and isolated is not None:
                        heading_match = match
                    elif heading_match is not None:
                        # A later chemical heading or an unproven mixed candidate
                        # wins over the earlier fallback. Only a demonstrable
                        # property/yield ID may leave that fallback in place.
                        cue = _bounded(_LAST_PROPERTY_CUE.search, prefix, budget)
                        if (isolated is not None or cue is None
                                or not _bounded(_PROPERTY_LABEL_TAIL.fullmatch, prefix[cue.end():], budget)
                                or not _bounded(_PROPERTY_IDENTIFIER.search, match.group(1), budget)):
                            heading_match = None
        except TimeoutError:
            budget.check()
            raise _ResolutionStopped("refused", "extraction_work_limit") from None
        valid_match = heading_match or valid_match
        if valid_match is not None:
            name = paragraph[:valid_match.start()].strip()
            identifier = valid_match.group(1)
            if name and (name, identifier) not in seen:
                seen.add((name, identifier))
                yield name, identifier, stat_key


def _smiles_and_name_from_paragraph(
    config: AppConfig,
    tracker: ErrorTracker | FindingSink,
    prev_paragraph,
    name_finding_statistics,
    log=None,
    *,
    cancel_token: threading.Event | None = None,
    details: dict | None = None,
    _session: _ResolverSession | None = None,
):
    """Return resolution facts, canonical diagnostics and compact outcome provenance."""
    budget = _Budget(_TIMEOUT_SECONDS, cancel_token)
    session = _session if _session is not None else _ResolverSession(log)
    outcome = {"status": "unresolved", "reason": None, "original_name": None,
               "accepted_name": None, "identifier": None, "transformations": []}
    smiles, display_name, identifier = None, "", ""
    warnings_found = []
    selected_stat_key = None
    shown_changes = []
    try:
        budget.check()
        if (len(prev_paragraph) > MAX_PARAGRAPH_BYTES
                or len(prev_paragraph.encode("utf-8")) > MAX_PARAGRAPH_BYTES):
            raise _ResolutionStopped("refused", "paragraph_size_limit")
        named, refused = [], []  # Each extraction is isolated or refused on its own.
        for prefix, ident, key in _extracted_names(prev_paragraph, budget):
            isolated = _isolated_name(prefix, budget)
            if isolated is None or len(isolated[0].encode("utf-8")) > MAX_NAME_BYTES:
                refused.append((prefix, ident, "mixed_context" if isolated is None else "name_size_limit"))
            else:
                named.append((prefix, *isolated, ident, key))
        if refused and not named:
            # Overlapping identifier patterns must not repeat a refused prefix.
            original, identifier, reason = max(refused, key=lambda item: len(item[0]))
            outcome.update(original_name=original, identifier=identifier)
            raise _ResolutionStopped("refused", reason)
        if named:
            display_name = ", ".join(item[1] for item in named)
            identifier = ", ".join(item[3] for item in named)
            shown_changes = [{"kind": "isolate_name_segment"}] if any(item[2] for item in named) else []
            outcome.update(original_name=", ".join(item[0] for item in named), identifier=identifier,
                           transformations=shown_changes)
        budget.check()
        if config.modes.deep_name_analysis != DeepNameAnalysis.ON:
            outcome["status"] = "disabled"
        else:
            # Round-robin the extraction streams: all originals first, then
            # mechanical/prose variants; speculative edits remain last.
            candidates = [(candidate, changes, item, rank) for item in named
                          for rank, (candidate, changes) in enumerate(_name_candidates(item[1], budget))]
            candidates.sort(key=lambda entry: (
                any(change["kind"] == "remove_single_hyphen" for change in entry[1]), entry[3],
            ))
            seen = set()
            for candidate, changes, (prefix, name, isolation, ident, key), _ in candidates:
                if not candidate or candidate in seen:
                    continue
                if len(seen) >= _MAX_ATTEMPTS:
                    raise _ResolutionStopped("budget_exhausted", "candidate_limit")
                seen.add(candidate)
                smiles = session.resolve(candidate, budget)
                budget.check()
                if session.local_unavailable:
                    raise _ResolutionStopped("failed", "local_unavailable")
                if smiles:
                    display_name, identifier, selected_stat_key = name, ident, key
                    # Isolation alone keeps "resolved": the displayed name was dispatched.
                    outcome.update(status="transformed" if changes else "resolved", original_name=prefix,
                                   accepted_name=candidate, identifier=ident, transformations=isolation + changes)
                    break
    except _ResolutionStopped as stopped:
        smiles = None
        outcome.update(status=stopped.status, reason=stopped.reason)
        if stopped.status == "refused":
            display_name = ""
        # Refusals and candidate caps leave the run-owned worker idle and reusable.
        if stopped.reason in {"deadline", "cancel_requested"} and not session.close():
            outcome["reason"] = "worker_cleanup_failed"
    except (OSError, EOFError, ValueError, TypeError, RuntimeError):
        smiles = None
        outcome.update(status="failed", reason="worker_failure")
        session.close()
    finally:
        if _session is None and not session.close():
            smiles = None
            outcome.update(status="failed", reason="worker_cleanup_failed")
    if cancel_token is not None and cancel_token.is_set():
        smiles = None
        outcome.update(status="cancelled", reason="cancel_requested")
    if smiles and selected_stat_key:
        name_finding_statistics[selected_stat_key] = name_finding_statistics.get(selected_stat_key, 0) + 1
    else:
        outcome.update(accepted_name=None, transformations=shown_changes if display_name else [])
    if outcome["status"] == "transformed" and smiles:
        warning = emit_finding(config, tracker, ErrorCode.NAME_MODIFIED_FOR_SMILES,
                             context={"resolved_name": outcome["accepted_name"]})
        if warning:
            warnings_found.append(warning)
    elif outcome["status"] == "refused":
        # Nothing was looked up; the caller reports the missing name once.
        if log:
            log(f"Name extraction refused: {outcome['reason']}.")
    elif outcome["status"] not in {"cancelled", "disabled", "resolved"} and display_name:
        for variant, context in (("parsed_name", {"resolved_name": display_name, "identifier": identifier}),
                                 ("resolve_failed", {})):
            warning = emit_finding(config, tracker, ErrorCode.NAME_TO_SMILES_FAILED, context=context, variant=variant)
            if warning:
                warnings_found.append(warning)
    if log and outcome["status"] != "refused":
        log(f"Parsed name: {display_name or '(none)'} ({identifier or '?'}) | SMILES: {smiles or '(unresolved)'}")
    if details is not None:
        details.clear()
        details.update(outcome)
    return smiles, display_name, identifier, name_finding_statistics, warnings_found


def smiles_and_name_from_paragraph(
    config: AppConfig,
    tracker: ErrorTracker,
    prev_paragraph,
    name_finding_statistics,
    log=None,
    *,
    cancel_token: threading.Event | None = None,
    details: dict | None = None,
    _session: _ResolverSession | None = None,
):
    """Return the legacy five-tuple and optionally fill compact outcome provenance."""
    smiles, name, identifier, statistics, diagnostics = _smiles_and_name_from_paragraph(
        config, tracker, prev_paragraph, name_finding_statistics, log,
        cancel_token=cancel_token, details=details, _session=_session,
    )
    return smiles, name, identifier, statistics, messages(diagnostics)


if __name__ == "__main__":
    _worker_main()
