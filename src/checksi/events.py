"""Structured runtime events for CLI and GUI consumers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from checksi.config_schema import ErrorSeverity


@dataclass(frozen=True, slots=True)
class LogEvent:
    """Single log message with explicit severity metadata."""

    severity: ErrorSeverity
    message: str


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """Phase-local progress; total <= 0 denotes an indeterminate transition.

    Counters describe the current operation, never an overall run percentage.
    """

    current: int
    total: int
    message: str = ""


ApplicationEvent = LogEvent | ProgressEvent
EventDispatcher = Callable[[ApplicationEvent], None]
LegacyLogCallback = Callable[[str], None]
LegacyProgressCallback = Callable[[int, int, str], None]


def infer_log_severity(message: str) -> ErrorSeverity:
    """Map legacy string logs to the best-guess severity."""
    text = message.strip().lower()
    if "error" in text or "failed" in text or "traceback" in text or "❌" in message:
        return ErrorSeverity.CRITICAL
    if "warning" in text or "⚠️" in message or "❗" in message:
        return ErrorSeverity.WARNING
    return ErrorSeverity.INFO


def create_event_dispatcher(
    event_dispatcher: EventDispatcher | None = None,
    *,
    log_callback: LegacyLogCallback | None = None,
    progress_callback: LegacyProgressCallback | None = None,
) -> EventDispatcher:
    """Normalize legacy callbacks into one event dispatcher."""
    if event_dispatcher is not None:
        return event_dispatcher

    _log = log_callback or print
    _progress = progress_callback or (lambda current, total, message="": None)

    def _dispatch(event: ApplicationEvent) -> None:
        if isinstance(event, LogEvent):
            _log(event.message)
            return
        if isinstance(event, ProgressEvent):
            _progress(event.current, event.total, event.message)

    return _dispatch
