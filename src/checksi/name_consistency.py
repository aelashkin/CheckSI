"""One note per compound when independent checks all disagree with the structure taken from the name."""

from __future__ import annotations

import re
from collections import Counter

from checksi.config_schema import AppConfig, ErrorSeverity
from checksi.errors import ErrorCode
from checksi.errors.findings import highlight_color, messages
from checksi.errors.manager import emit_finding, format_finding
from checksi.errors.run_diagnostics import RunDiagnostics
from checksi.events import EventDispatcher, LogEvent
from checksi.utils.error_handler import ErrorTracker

# Independent comparisons with the structure that the name gives. Two 1H findings come from one
# model and count once.
_GROUPS = {
    'name_formula_mismatch': 'the HRMS or EA formula',
    'h1_shift_structure_mismatch': 'the 1H shifts',
    'h1_protons_unaccounted': 'the 1H shifts',
    'dp5_low_probability': 'the 13C shifts',
}
_CODE = re.compile(r'^\W*\s*([a-z0-9_]+)\.\s')


def disagreeing_checks(warnings: list[str]) -> list[str]:
    """What disagrees with the named structure, one entry per independent comparison."""
    found = []
    for warning in warnings:
        match = _CODE.match(warning)
        group = _GROUPS.get(match.group(1)) if match else None
        if group and group not in found:
            found.append(group)
    return found


def formula_written_as_neutral(compound: dict) -> bool:
    """True when the HRMS formula as written equals the formula of the name.

    Authors often write the neutral formula after an ion label ("[M+H]+ calcd for C17H18NO3P").
    Read as an ion it disagrees with the name by one adduct, but this is a notation defect that
    has its own findings; the name is not in doubt.
    """
    sources = compound.get('formula_sources') or {}

    def counts(key: str) -> dict | None:
        composition = (sources.get(key) or {}).get('composition') or {}
        return composition.get('counts') if composition.get('usable') else None

    written, named = counts('hrms'), counts('name')
    return bool(written) and written == named


def enrich_analysis_with_name_notes(cfg: AppConfig, analysis_data: dict, *,
                                    event_dispatcher: EventDispatcher | None = None,
                                    _diagnostics: RunDiagnostics | None = None) -> dict:
    """Add ``name_data_mismatch`` where two or more independent comparisons disagree with the name.

    The separate findings point at the data. When several of them fire together, the simpler
    explanation is often the name: a wrong locant or substituent, or a name the parser reads
    as another structure. This note says so; it removes nothing.
    """
    tracker = _diagnostics.tracker if _diagnostics is not None else ErrorTracker()
    count = Counter()
    for compound in analysis_data.get('compounds', []):
        if analysis_data.get('aborted'):
            break
        checks = disagreeing_checks(list(compound.get('all_warnings') or []))
        if formula_written_as_neutral(compound):
            checks = [check for check in checks if check != _GROUPS['name_formula_mismatch']]
        if len(checks) < 2:
            continue
        sink = (_diagnostics.sink(compound['id'], compound.get('source_paragraph_1based'), 'name', 'name')
                if _diagnostics is not None else tracker)
        warning = emit_finding(cfg, sink, ErrorCode.NAME_DATA_MISMATCH,
                               context={'checks': (', '.join(checks[:-1]) + ' and ' + checks[-1]).capitalize()
                                        .replace('1h', '1H').replace('13c', '13C').replace('hrms', 'HRMS').replace(' ea ', ' EA ')})
        count['name_data_mismatch'] += 1
        if _diagnostics is not None:
            display = list(_diagnostics.display(compound['id']))
            if warning is not None:
                display.append(warning)
            _diagnostics.set_display(compound['id'], display)
            compound['all_warnings'] = messages(display)
            compound.setdefault('highlight_info', {})['color'] = highlight_color(display)
        elif warning is not None:
            compound.setdefault('all_warnings', []).append(format_finding(warning))
    if _diagnostics is not None:
        analysis_data['error_stats'] = tracker.to_dict()
    else:
        stats = analysis_data.setdefault('error_stats', {})
        for key, value in tracker.to_dict().items():
            stats[key] = int(stats.get(key, 0)) + int(value)
    if event_dispatcher and count:
        event_dispatcher(LogEvent(severity=ErrorSeverity.INFO,
                                  message=f"Name consistency: name_data_mismatch={count['name_data_mismatch']}."))
    return analysis_data
