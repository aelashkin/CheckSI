"""Captured diagnostic occurrences and pure presentation projections."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Protocol, TYPE_CHECKING

from checksi.config_schema import ErrorSeverity
from checksi.errors.defs import AnalysisErrorCode

if TYPE_CHECKING:
    from checksi.errors.tracker import ErrorTracker


class ContextSnapshotError(TypeError):
    """An opaque legacy context value cannot become an isolated snapshot."""


@dataclass(frozen=True, slots=True, init=False)
class ContextSnapshot(Mapping[str, Any]):
    """A defensive value snapshot; nested reads never expose its owned values."""

    _values: Mapping[str, Any]

    def __init__(self, values: Mapping[str, Any]):
        try:
            copied = deepcopy(dict(values))
        except Exception as exc:
            raise ContextSnapshotError('Diagnostic context cannot be snapshotted') from exc
        object.__setattr__(self, '_values', MappingProxyType(copied))

    def __getitem__(self, key: str) -> Any:
        return deepcopy(self._values[key])

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __deepcopy__(self, memo):
        return self


@dataclass(frozen=True, slots=True)
class FindingSource:
    """Known analytical ownership without inferred document coordinates."""

    compound_id: int | None
    paragraph_1based: int | None
    stage: str
    nucleus: str | None = None


@dataclass(frozen=True, slots=True, eq=False)
class Finding:
    """One occurrence, including captured visibility and formatting inputs."""

    code: AnalysisErrorCode
    severity: ErrorSeverity
    variant: str
    context: ContextSnapshot
    source: FindingSource | None = None


@dataclass(frozen=True, slots=True)
class AnalysisNote:
    """Uncounted presentation text with an explicit optional highlight hint."""

    text: str
    highlight_hint: str | None = None


Diagnostic = Finding | AnalysisNote


class FindingRecorder(Protocol):
    """Record immutable references without requiring counters or export behavior."""

    def record(self, finding: Finding) -> None: ...


def message(diagnostic: Diagnostic | None) -> str | None:
    """Project an optional diagnostic slot in a legacy helper result."""
    projected = messages([diagnostic]) if diagnostic is not None else []
    return projected[0] if projected else None


@dataclass(frozen=True, slots=True)
class FindingSink:
    """An immutable source scope sharing the run's occurrence owner."""

    tracker: FindingRecorder
    source: FindingSource | None = None

    def record(self, finding: Finding) -> None:
        self.tracker.record(finding)


def scoped(tracker: ErrorTracker | FindingSink, stage: str,
           nucleus: str | None = None) -> FindingSink:
    """Change stage without mutating the shared run tracker or its source scope."""
    if isinstance(tracker, FindingSink):
        source = replace(tracker.source, stage=stage, nucleus=nucleus) if tracker.source else None
        return FindingSink(tracker.tracker, source)
    return FindingSink(tracker)


def messages(diagnostics: Sequence[Diagnostic]) -> list[str]:
    """Project ordered display references without changing their occurrences."""
    from checksi.errors.manager import format_finding

    return [format_finding(item) if isinstance(item, Finding) else item.text
            for item in diagnostics
            if not isinstance(item, Finding) or item.severity is not ErrorSeverity.DISABLED]


def highlight_color(diagnostics: Sequence[Diagnostic], *, fallback: str = 'blue') -> str:
    """Project captured severity and explicit note hints; red takes precedence."""
    if any(isinstance(item, Finding) and item.severity is ErrorSeverity.CRITICAL for item in diagnostics):
        return 'red'
    if any((isinstance(item, Finding) and item.severity is ErrorSeverity.WARNING)
           or (isinstance(item, AnalysisNote) and item.highlight_hint == 'yellow')
           for item in diagnostics):
        return 'yellow'
    return fallback


def issue_counts(occurrences: Sequence[Finding]) -> dict[str, int]:
    """Summarize effective severity snapshots rather than current settings."""
    counts = {level: sum(item.severity.value == level for item in occurrences)
              for level in ('info', 'warning', 'critical')}
    return {**counts, 'anomaly_count': counts['warning'] + counts['critical']}
