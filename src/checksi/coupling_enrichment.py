"""Coupling-constant findings for 1H and 13C descriptions (always on, no model needed)."""

from __future__ import annotations

import re
from collections import Counter

from checksi.config_schema import AppConfig, ErrorSeverity
from checksi.couplings.checks import carbon_issues, proton_issues, structure_facts
from checksi.couplings.parsing import parse_signals
from checksi.errors import ErrorCode
from checksi.errors.findings import highlight_color, messages
from checksi.errors.manager import emit_finding, format_finding
from checksi.errors.run_diagnostics import RunDiagnostics
from checksi.events import EventDispatcher, LogEvent
from checksi.utils.error_handler import ErrorTracker

_CODES = {
    'hnmr_j_partner_missing': ErrorCode.HNMR_J_PARTNER_MISSING,
    'hnmr_j_partner_mismatch': ErrorCode.HNMR_J_PARTNER_MISMATCH,
    'hnmr_j_value_implausible': ErrorCode.HNMR_J_VALUE_IMPLAUSIBLE,
    'hnmr_heteronuclear_coupling_missing': ErrorCode.HNMR_HETERONUCLEAR_COUPLING_MISSING,
    'c13_cf_j_value_implausible': ErrorCode.C13_CF_J_VALUE_IMPLAUSIBLE,
    'c13_cf_multiplicity_mismatch': ErrorCode.C13_CF_MULTIPLICITY_MISMATCH,
    'c13_cf_coupling_incomplete': ErrorCode.C13_CF_COUPLING_INCOMPLETE,
    'fnmr_decoupling_not_stated': ErrorCode.FNMR_DECOUPLING_NOT_STATED,
    'c13_cf_long_range_coupling_missing': ErrorCode.C13_CF_LONG_RANGE_COUPLING_MISSING,
}
_FLUORINE_SECTION = re.compile(r'19\s*F[^δ]{0,80}δ.*?(?=\b(?:1H|13C|31P|11B|29Si|HRMS|HR-MS|IR|MS|Anal)\b|$)', re.S)


_FORMULA = re.compile(r'\bC\d+H\d+(?:[A-Z][a-z]?\d*)*')


def _mentions_fluorine_or_phosphorus(text: str) -> bool:
    """A 19F/31P spectrum, or F/P in a molecular formula of the description (HRMS, EA)."""
    if re.search(r'19\s*F|31\s*P', text):
        return True
    return any(set(re.findall(r'[A-Z][a-z]?', formula)) & {'F', 'P'} for formula in _FORMULA.findall(text))


def _fluorine_section(text: str) -> str | None:
    match = _FLUORINE_SECTION.search(text or '')
    return match.group(0) if match else None


def enrich_analysis_with_couplings(cfg: AppConfig, analysis_data: dict, *,
                                   event_dispatcher: EventDispatcher | None = None,
                                   _diagnostics: RunDiagnostics | None = None) -> dict:
    """Add coupling-constant findings to every compound with a 1H or 13C section."""
    tracker = _diagnostics.tracker if _diagnostics is not None else ErrorTracker()
    outcome = Counter()

    def finding(compound, nucleus, issue):
        sink = (_diagnostics.sink(compound['id'], compound.get('source_paragraph_1based'), 'couplings', nucleus)
                if _diagnostics is not None else tracker)
        warning = emit_finding(cfg, sink, _CODES[issue.code], context=issue.context, variant=issue.variant)
        outcome[issue.code] += 1
        if _diagnostics is not None:
            display = list(_diagnostics.display(compound['id']))
            if warning is not None:
                display.append(warning)
            _diagnostics.set_display(compound['id'], display)
            compound['all_warnings'] = messages(display)
            compound.setdefault('highlight_info', {})['color'] = highlight_color(display)
        elif warning is not None:
            compound.setdefault('all_warnings', []).append(format_finding(warning))

    for compound in analysis_data.get('compounds', []):
        if analysis_data.get('aborted'):
            break
        info = compound.get('highlight_info') or {}
        proton, carbon = info.get('hnmr_text'), info.get('cnmr_text')
        if not proton and not carbon:
            continue
        text = compound.get('original_text') or ''
        facts = structure_facts(compound.get('smiles'))
        if proton:
            signals = parse_signals(proton)
            hetero = _mentions_fluorine_or_phosphorus(text)
            for issue in proton_issues(signals, facts, has_fluorine_or_phosphorus=hetero,
                                       fluorine_section=_fluorine_section(text)):
                finding(compound, '1H', issue)
        if carbon and facts is not None:
            for issue in carbon_issues(parse_signals(carbon), facts, carbon):
                finding(compound, '13C', issue)

    if _diagnostics is not None:
        analysis_data['error_stats'] = tracker.to_dict()
    else:
        stats = analysis_data.setdefault('error_stats', {})
        for key, value in tracker.to_dict().items():
            stats[key] = int(stats.get(key, 0)) + int(value)
    if event_dispatcher and outcome:
        event_dispatcher(LogEvent(severity=ErrorSeverity.INFO, message='Coupling-constant checks: '
                                  + ', '.join(f'{key}={value}' for key, value in sorted(outcome.items())) + '.'))
    return analysis_data
