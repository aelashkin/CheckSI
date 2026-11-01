"""Pure per-run diagnostic counts and legacy counter mutations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from checksi.errors.defs import AnalysisErrorCode

if TYPE_CHECKING:
    from checksi.errors.findings import Finding


ERROR_NAMES = [error_code.value for error_code in AnalysisErrorCode]


class ErrorTracker:
    """Per-run owner of error counters."""

    def __init__(self):
        self._legacy_counts = {error: 0 for error in ERROR_NAMES}
        self._occurrences: list[Finding] = []

    @property
    def occurrences(self) -> tuple[Finding, ...]:
        """Return occurrences in emission order, preserving repeated identities."""
        return tuple(self._occurrences)

    def record(self, finding: Finding) -> None:
        """Record a full occurrence without invoking the count-only adapter."""
        self._occurrences.append(finding)

    def add(self, error_name: str) -> None:
        if error_name not in self._legacy_counts:
            raise ValueError(f"Unknown error code: {error_name}")
        self._legacy_counts[error_name] += 1

    def get_count(self, error_name: str) -> int:
        if error_name not in self._legacy_counts:
            raise ValueError(f"Unknown error code: {error_name}")
        return self.to_dict()[error_name]

    def to_dict(self) -> dict:
        counts = dict(self._legacy_counts)
        for finding in self._occurrences:
            if finding.severity.value != 'disabled':
                counts[finding.code.value] += 1
        return counts


def add_error(tracker: ErrorTracker, error_name: str) -> None:
    """Record an error via the provided run-scoped tracker."""
    tracker.add(error_name)
