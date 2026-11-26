"""Optional advisory comparison of reported 1H shifts with CASCADE predictions."""

from __future__ import annotations

from collections import Counter

from checksi.config_schema import AppConfig, ErrorSeverity
from checksi.dp5q.contracts import CalculationCancelled
from checksi.errors import ErrorCode
from checksi.errors.findings import highlight_color, messages
from checksi.errors.manager import emit_finding, format_finding
from checksi.errors.run_diagnostics import RunDiagnostics
from checksi.events import EventDispatcher, LogEvent, ProgressEvent
from checksi.h1shift.comparison import (AROMATIC_SOLVENT_MEAN_EXTRA_PPM, AROMATIC_SOLVENT_SIGNAL_EXTRA_PPM,
                                        aromatic_solvent, compare, multiplicity_issues, parse_signals,
                                        reported_protons)
from checksi.h1shift.model import DomainError, exchangeable_protons, predict_environments
from checksi.utils.error_handler import ErrorTracker


def enrich_analysis_with_h1_shifts(cfg: AppConfig, analysis_data: dict, *,
                                   event_dispatcher: EventDispatcher | None = None,
                                   cancel_token=None,
                                   _diagnostics: RunDiagnostics | None = None) -> dict:
    """Add advisory 1H shift findings to compounds with a structure and integrated 1H data."""
    settings = cfg.h1_shift
    if not settings.enabled:
        return analysis_data
    tracker = _diagnostics.tracker if _diagnostics is not None else ErrorTracker()
    compounds = list(analysis_data.get('compounds', []))
    outcome = Counter()

    def finding(compound, code, context, variant='default'):
        sink = (_diagnostics.sink(compound['id'], compound.get('source_paragraph_1based'), 'h1_shift', '1H')
                if _diagnostics is not None else tracker)
        warning = emit_finding(cfg, sink, code, context=context, variant=variant)
        if _diagnostics is not None:
            display = list(_diagnostics.display(compound['id']))
            if warning is not None:
                display.append(warning)
            _diagnostics.set_display(compound['id'], display)
            compound['all_warnings'] = messages(display)
            compound.setdefault('highlight_info', {})['color'] = highlight_color(display)
        elif warning is not None:
            compound.setdefault('all_warnings', []).append(format_finding(warning))

    _log(event_dispatcher, 'Starting 1H shift comparison...')
    for index, compound in enumerate(compounds, start=1):
        if analysis_data.get('aborted') or (cancel_token is not None and cancel_token.is_set()):
            compound['h1_shift'] = {'status': 'skipped', 'reason': 'cancelled'}
            outcome['cancelled'] += 1
            continue
        if event_dispatcher:
            event_dispatcher(ProgressEvent(index, len(compounds), '1H shift comparison'))
        smiles = compound.get('smiles')
        section = (compound.get('highlight_info') or {}).get('hnmr_text')
        if not smiles or not section:
            compound['h1_shift'] = {'status': 'skipped', 'reason': 'no_structure_or_1h'}
            outcome['no_structure_or_1h'] += 1
            continue
        signals = parse_signals(section)
        if signals is None:
            compound['h1_shift'] = {'status': 'skipped', 'reason': 'integrals_missing'}
            outcome['integrals_missing'] += 1
            continue
        # Aromatic solvents move every shift; both thresholds are raised for them.
        in_aromatic_solvent = aromatic_solvent(section)
        mean_threshold = settings.mean_error_threshold_ppm + (AROMATIC_SOLVENT_MEAN_EXTRA_PPM if in_aromatic_solvent else 0)
        signal_threshold = settings.signal_error_threshold_ppm + (AROMATIC_SOLVENT_SIGNAL_EXTRA_PPM if in_aromatic_solvent else 0)
        try:
            environments = predict_environments(smiles, cancel_check=_cancel_check(cancel_token))
            if not environments:
                raise DomainError('the structure has no C-H protons')
            exchangeable = exchangeable_protons(smiles)
            result = compare(environments, signals, signal_threshold, exchangeable)
        except CalculationCancelled:
            compound['h1_shift'] = {'status': 'skipped', 'reason': 'cancelled'}
            outcome['cancelled'] += 1
            continue
        except (DomainError, ValueError) as exc:
            compound['h1_shift'] = {'status': 'skipped', 'reason': 'not_applicable', 'details': str(exc)}
            outcome['not_applicable'] += 1
            finding(compound, ErrorCode.H1_SHIFT_NOT_APPLICABLE, {'details': str(exc)})
            continue
        compound['h1_shift'] = {
            'status': 'completed', 'mean_error_ppm': round(result.mean_error, 3),
            'matched_protons': result.matched,
            'predicted': [{'shift': round(env.shift, 2), 'count': env.count,
                           'atoms': [i + 1 for i in env.atom_indices],
                           **({'unreliable': env.unreliable} if env.unreliable else {})} for env in environments],
        }
        if result.unreliable:
            compound['h1_shift']['unreliable_protons'] = result.unreliable
        if result.exchangeable_slots:
            compound['h1_shift']['signals_taken_by_exchangeable_protons'] = result.exchangeable_slots
        if in_aromatic_solvent:
            compound['h1_shift']['aromatic_solvent'] = True
        outcome['completed'] += 1
        # Missing protons: the usual excuse is "exchangeable, not listed". The matching tests it:
        # the excuse holds only when every predicted C-H proton found a signal.
        carbon_bound = sum(env.count for env in environments)
        reported = reported_protons(signals)
        total = carbon_bound + exchangeable
        mixture = any(signal.fractional for signal in signals)
        if reported < total and not mixture:
            compound['h1_shift']['missing_protons'] = {
                'reported': reported, 'structure': total, 'carbon_bound': carbon_bound,
                'exchangeable': exchangeable, 'unaccounted_ch': len(result.unaccounted),
                'explained_by_exchangeable': not result.unaccounted and total - reported <= exchangeable}
            if result.unaccounted:
                finding(compound, ErrorCode.H1_PROTONS_UNACCOUNTED,
                        {'count': len(result.unaccounted),
                         'shifts': ', '.join(f'{value:.2f}' for value in result.unaccounted[:6])
                                   + (', ...' if len(result.unaccounted) > 6 else ''),
                         'threshold': f'{signal_threshold:g}', 'reported': reported,
                         'total': total, 'carbon_bound': carbon_bound, 'exchangeable': exchangeable})
        if result.mean_error > mean_threshold:
            finding(compound, ErrorCode.H1_SHIFT_STRUCTURE_MISMATCH,
                    {'mean_error': f'{result.mean_error:.2f}', 'threshold': f'{mean_threshold:g}'})
        for signal, predicted in result.outliers:
            finding(compound, ErrorCode.H1_SHIFT_SIGNAL_OUTLIER,
                    {'signal': signal.text, 'predicted': f'{predicted:.2f}',
                     'threshold': f'{signal_threshold:g}'})
        for issue in multiplicity_issues(environments, signals, result):
            finding(compound, ErrorCode.H1_MULTIPLICITY_STRUCTURE_MISMATCH,
                    {'signal': issue.signal.text, 'available': issue.available, 'protons': issue.signal.protons},
                    variant={'must_split': 'must_split', 'partly_possible': 'partly_possible'}.get(issue.reason, 'default'))

    if _diagnostics is not None:
        analysis_data['error_stats'] = tracker.to_dict()
    else:
        stats = analysis_data.setdefault('error_stats', {})
        for key, value in tracker.to_dict().items():
            stats[key] = int(stats.get(key, 0)) + int(value)
    analysis_data['h1_shift_summary'] = {'enabled': True, 'total_compounds': len(compounds), **outcome}
    _log(event_dispatcher, '1H shift comparison complete: '
         + ', '.join(f'{key}={value}' for key, value in sorted(outcome.items())) + '.')
    return analysis_data


def _cancel_check(cancel_token):
    """Stop geometry generation between conformers when the run is cancelled."""
    if cancel_token is None:
        return None

    def check() -> None:
        if cancel_token.is_set():
            raise CalculationCancelled('1H shift comparison cancelled.')
    return check


def _log(event_dispatcher, message: str, severity: ErrorSeverity = ErrorSeverity.INFO) -> None:
    if event_dispatcher:
        event_dispatcher(LogEvent(severity=severity, message=message))
