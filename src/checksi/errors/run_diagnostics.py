"""Explicit run-owned diagnostics beside the unchanged public result dictionary."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from checksi.config_schema import AppConfig, ErrorSeverity
from checksi.errors.defs import AnalysisErrorCode
from checksi.errors.findings import Diagnostic, Finding, FindingSink, FindingSource
from checksi.errors.manager import get_error_severity
from checksi.errors.tracker import ErrorTracker


@dataclass(frozen=True, slots=True)
class DiagnosticSnapshot:
    """Occurrence order and separate display order reference the same records."""

    occurrences: tuple[Finding, ...]
    displays: Mapping[int, tuple[Diagnostic, ...]]
    severities: Mapping[str, str]


def _validate_display(occurrences: Sequence[Finding], displays: Mapping[int, Sequence[Diagnostic]]) -> None:
    """Check visible identity/multiplicity together with its compound ownership."""
    expected = Counter()
    for item in occurrences:
        if item.severity is not ErrorSeverity.DISABLED:
            if item.source is None or item.source.compound_id is None:
                raise ValueError('Visible Finding has no canonical compound ownership')
            expected[item.source.compound_id, item] += 1
    displayed = Counter()
    for compound_id, items in displays.items():
        for item in items:
            if isinstance(item, Finding) and item.severity is not ErrorSeverity.DISABLED:
                if item.source is None or item.source.compound_id != compound_id:
                    raise ValueError('Finding source and display compound ownership differ')
                displayed[compound_id, item] += 1
    if expected != displayed:
        raise ValueError('Visible occurrences and diagnostic display references differ')


class _PendingCompound:
    """Unpublished references for one compound; no independent count authority."""

    def __init__(self, run: RunDiagnostics, compound_id: int):
        self._run = run
        self._compound_id = compound_id
        self._occurrences: list[Finding] = []
        self._committed = False

    def sink(self, paragraph_1based: int | None, stage: str,
             nucleus: str | None = None) -> FindingSink:
        return FindingSink(self, FindingSource(self._compound_id, paragraph_1based, stage, nucleus))

    def record(self, finding: Finding) -> None:
        if self._committed:
            raise RuntimeError('Compound diagnostics have already been committed')
        self._occurrences.append(finding)

    def commit(self, diagnostics: Sequence[Diagnostic]) -> None:
        """Validate first, then publish the same objects exactly once in emission order."""
        if self._committed:
            raise RuntimeError('Compound diagnostics have already been committed')
        if self._compound_id in self._run._displays:
            raise ValueError('Compound diagnostics already have a committed display')
        display = list(diagnostics)
        _validate_display(self._occurrences, {self._compound_id: display})
        for finding in self._occurrences:
            self._run.tracker.record(finding)
        self._run.set_display(self._compound_id, display)
        self._committed = True


class RunDiagnostics:
    """One mutable builder per application run; never stored on configuration."""

    def __init__(self, config: AppConfig):
        self.tracker = ErrorTracker()
        self.analysis_recorded = False
        self._displays: dict[int, list[Diagnostic]] = {}
        self._severities = {code.value: get_error_severity(config, code).value
                            for code in AnalysisErrorCode}

    def sink(self, compound_id: int, paragraph_1based: int | None,
             stage: str, nucleus: str | None = None) -> FindingSink:
        return FindingSink(self.tracker, FindingSource(compound_id, paragraph_1based, stage, nucleus))

    def begin_compound(self, compound_id: int) -> _PendingCompound:
        """Stage records locally until the controller accepts the complete compound."""
        if compound_id in self._displays:
            raise ValueError('Compound diagnostics already have a committed display')
        return _PendingCompound(self, compound_id)

    def set_display(self, compound_id: int, diagnostics: Sequence[Diagnostic]) -> None:
        self._displays[compound_id] = list(diagnostics)

    def display(self, compound_id: int) -> tuple[Diagnostic, ...]:
        return tuple(self._displays.get(compound_id, ()))

    def complete_analysis(self) -> None:
        """Identify results produced by the typed controller rather than legacy adapters."""
        self.snapshot()
        self.analysis_recorded = True

    def snapshot(self) -> DiagnosticSnapshot:
        """Freeze and verify every actual visible occurrence has one display slot."""
        occurrences = self.tracker.occurrences
        _validate_display(occurrences, self._displays)
        return DiagnosticSnapshot(occurrences,
                                  MappingProxyType({key: tuple(items) for key, items in self._displays.items()}),
                                  MappingProxyType(dict(self._severities)))
