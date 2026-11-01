"""Adapt completed analysis to one lazy, isolated DP5q worker per run."""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path

from pydantic import ValidationError
from rdkit import Chem

from checksi.config_schema import AppConfig, ErrorSeverity, DeepNameAnalysis
from checksi.dp5q.client import (DP5qCancelled, DP5qSession, DP5qWorkerError,
                                     DP5qWorkerUnavailable, check_availability as check_availability)
from checksi.dp5q.contracts import (CheckRequest, CheckResult, MAX_ATOMS,
                                        MAX_GROUPS, MAX_SMILES_LENGTH)
from checksi.dp5q.components import ComponentSelectionError, ScoringComponent, select_scoring_component
from checksi.dp5q.observation_policy import BORON_REASON, derive_observation_policy, has_boron
from checksi.dp5q.assignment import AssignmentError
from checksi.analysis.structure_analysis import analyze_molecular_structure_detailed
from checksi.dp5q.observation_assignment import validate_observation_coverage
from checksi.errors import ErrorCode
from checksi.errors.findings import highlight_color, messages
from checksi.errors.manager import emit_finding, format_finding
from checksi.errors.run_diagnostics import RunDiagnostics
from checksi.events import EventDispatcher, LogEvent, ProgressEvent
from checksi.file_io import write_dp5q_artifacts
from checksi.utils.dp5_format import format_score_and_threshold
from checksi.utils.error_handler import ErrorTracker


class DP5PreparationSkip(RuntimeError):
    """A parser or adapter coverage refusal with an existing finding identity."""

    def __init__(self, reason, *, warning_details=None, info_message=None,
                 warning_code=ErrorCode.DP5_NOT_APPLICABLE, warning_context=None):
        self.reason = reason
        self.warning_details = warning_details
        self.info_message = info_message
        self.warning_code = warning_code
        self.warning_context = warning_context
        super().__init__(reason)


def prepare_dp5_requests(cfg: AppConfig, analysis_data: dict, *, cancel_token=None
                         ) -> tuple[CheckRequest | str | DP5PreparationSkip, ...]:
    """Prepare applicability once without findings, mutation or worker readiness."""
    prepared = []
    for index, compound in enumerate(analysis_data.get('compounds', []), start=1):
        if analysis_data.get('aborted') or (cancel_token is not None and cancel_token.is_set()):
            prepared.append('cancelled')
            continue
        try:
            prepared.append(_compound_request(cfg, compound, index))
        except DP5PreparationSkip as exc:
            prepared.append(exc)
    return tuple(prepared)


def enrich_analysis_with_dp5(cfg: AppConfig, analysis_data: dict, *,
                             event_dispatcher: EventDispatcher | None = None,
                             cancel_token=None, runtime_info: dict | None = None,
                             prepared_requests: tuple[CheckRequest | str | DP5PreparationSkip, ...] | None = None,
                             _diagnostics: RunDiagnostics | None = None) -> dict:
    """Retain completed results while supervising serial checks in one session."""
    if not cfg.dp5.enabled:
        return analysis_data
    # The session owns lazy readiness. A run containing only unsupported B
    # structures never needs model assets or a sidecar to explain its skips.
    available = dict(runtime_info or {})
    tracker = _diagnostics.tracker if _diagnostics is not None else ErrorTracker()
    compounds = list(analysis_data.get("compounds", []))
    if prepared_requests is not None and len(prepared_requests) != len(compounds):
        raise ValueError('Prepared DP5q requests do not match the compound list.')
    scores = []
    skipped = Counter()
    failed = 0
    unavailable_reason = (str(available.get('reason') or 'runtime unavailable')
                          if available.get('available') is False else None)
    _dispatch_log(event_dispatcher, "Starting DP5q enrichment...")

    def finding(compound, code, context, *, variant='default'):
        sink = (_diagnostics.sink(compound['id'], compound.get('source_paragraph_1based'), 'dp5')
                if _diagnostics is not None else tracker)
        warning = emit_finding(cfg, sink, code, context=context, variant=variant)
        if _diagnostics is not None:
            display = list(_diagnostics.display(compound['id']))
            if warning is not None:
                display.append(warning)
            _diagnostics.set_display(compound['id'], display)
            compound['all_warnings'] = messages(display)
            compound.setdefault('highlight_info', {})['color'] = highlight_color(display)
        else:
            if warning is not None:
                compound.setdefault("all_warnings", []).append(format_finding(warning))
            _refresh_highlight_color(compound)

    with DP5qSession(cfg.dp5, event_dispatcher, cancel_token) as session:
        for index, compound in enumerate(compounds, start=1):
            cancelled = bool(cancel_token is not None and cancel_token.is_set())
            if cancelled or analysis_data.get("aborted"):
                analysis_data["aborted"] = True
                compound["dp5"] = {"status": "skipped", "reason": "cancelled"}
                skipped["cancelled"] += 1
                continue
            try:
                request = (_compound_request(cfg, compound, index) if prepared_requests is None
                           else prepared_requests[index - 1])
                if isinstance(request, DP5PreparationSkip):
                    raise request
                if isinstance(request, str):
                    compound["dp5"] = {"status": "skipped", "reason": request}
                    skipped[request] += 1
                    continue
                if unavailable_reason is not None:
                    details = f"DP5q was not run because its worker is unavailable: {unavailable_reason}"
                    compound["dp5"] = {"status": "failed", "reason": "worker_unavailable", "error": details}
                    failed += 1
                    finding(compound, ErrorCode.DP5_ANALYSIS_FAILED, {"details": details})
                    continue
                result = session.check(request)
                available.update(session.metadata)
            except DP5PreparationSkip as exc:
                compound["dp5"] = {"status": "skipped", "reason": exc.reason}
                skipped[exc.reason] += 1
                if exc.info_message is not None:
                    compound["dp5"].update(message=exc.info_message, message_severity="info")
                else:
                    finding(compound, exc.warning_code, exc.warning_context or {"details": exc.warning_details})
                continue
            except DP5qCancelled:
                analysis_data["aborted"] = True
                if cancel_token is not None:
                    cancel_token.set()
                compound["dp5"] = {"status": "skipped", "reason": "cancelled"}
                skipped["cancelled"] += 1
                continue
            except KeyboardInterrupt:
                if cancel_token is None:
                    raise
                cancel_token.set()
                analysis_data["aborted"] = True
                compound["dp5"] = {"status": "skipped", "reason": "cancelled"}
                skipped["cancelled"] += 1
                continue
            except DP5qWorkerError as exc:
                if exc.diagnostic_detail:
                    _dispatch_log(event_dispatcher,
                                  f"DP5q worker details ({exc.reason}):\n{exc.diagnostic_detail}",
                                  ErrorSeverity.WARNING)
                if exc.stderr_tail:
                    _dispatch_log(event_dispatcher,
                                  f"DP5q worker diagnostics ({exc.reason}):\n{exc.stderr_tail}",
                                  ErrorSeverity.WARNING)
                if isinstance(exc, DP5qWorkerUnavailable):
                    unavailable_reason = str(exc)
                    _dispatch_log(event_dispatcher,
                                  "DP5q is unavailable; remaining eligible compounds will be marked as failed. "
                                  "Run `dp5 doctor` to diagnose the installation.", ErrorSeverity.WARNING)
                failed += 1
                compound["dp5"] = {"status": "failed", "reason": exc.reason, "error": str(exc)}
                finding(compound, ErrorCode.DP5_ANALYSIS_FAILED, {"details": str(exc)})
                continue
            finally:
                _dispatch_progress(event_dispatcher, index, len(compounds),
                                   f"Completed DP5q {index}/{len(compounds)}")

            if result.status != "assessed":
                compound["dp5"] = {"status": "skipped" if result.status == "not_applicable" else "failed",
                                   "reason": result.reason, "diagnostics": result.diagnostics,
                                   "provenance": result.provenance, "timings": result.timings}
                if result.status == "not_applicable":
                    skipped[result.reason] += 1
                    finding(compound, ErrorCode.DP5_BORON_NOT_SUPPORTED if result.reason == BORON_REASON else ErrorCode.DP5_NOT_APPLICABLE,
                            {"details": result.diagnostics.get("message", result.reason)})
                else:
                    failed += 1
                    finding(compound, ErrorCode.DP5_ANALYSIS_FAILED,
                            {"details": result.diagnostics.get("message", result.reason)})
                continue
            enriched = _adapt_result(result)
            compound["dp5"] = enriched
            scores.append(enriched["score_percent"])
            if result.provenance.get('component_scope'):
                finding(compound, ErrorCode.DP5_CONFIGURATION_DEPENDENT, {}, variant='organic_component')
            unverified = (result.provenance.get('stereo_verification_complete') is False
                          or result.provenance.get('coordinate_partition_verified') is False)
            if result.provenance.get('configuration_dependent') is True or unverified:
                finding(compound, ErrorCode.DP5_CONFIGURATION_DEPENDENT,
                        {'selected_isomer_smiles': result.provenance['selected_isomer_smiles']},
                        variant='unverified_geometry' if unverified else 'default')
            if cfg.dp5.keep_artifacts:
                try:
                    enriched["artifact_dir"] = str(write_dp5q_artifacts(
                        Path(cfg.paths.output_dir), index,
                        request.model_dump(mode="json"), result.model_dump(mode="python")))
                except (OSError, TypeError, ValueError) as exc:
                    enriched["artifact_warning"] = (
                        f"The assessment succeeded but its optional artifacts could not be saved: {exc}")
                    _dispatch_log(event_dispatcher, enriched["artifact_warning"], ErrorSeverity.WARNING)
            if enriched["score_percent"] < cfg.dp5.score_threshold_percent:
                score_text, threshold_text = format_score_and_threshold(
                    enriched["score_percent"], cfg.dp5.score_threshold_percent)
                finding(compound, ErrorCode.DP5_LOW_PROBABILITY,
                        {"probability_percent": enriched["score_percent"],
                         "threshold_percent": cfg.dp5.score_threshold_percent,
                         "score_text": score_text, "threshold_text": threshold_text})

    if _diagnostics is not None:
        analysis_data['error_stats'] = tracker.to_dict()
    else:
        _merge_error_stats(analysis_data.setdefault("error_stats", {}), tracker.to_dict())
    summary = {"enabled": True, "total_compounds": len(compounds), "successful": len(scores),
               "failed": failed, "skipped": sum(skipped.values()), "skipped_reasons": dict(skipped),
               "n": len(scores), "model_id": available.get("model_id"),
               "score_version": available.get("score_version")}
    for name, value in (("min", min(scores) if scores else None),
                        ("max", max(scores) if scores else None),
                        ("mean", sum(scores) / len(scores) if scores else None)):
        summary[f"{name}_score_percent"] = value
        summary[f"{name}_probability_percent"] = value  # Transitional report-data alias.
    analysis_data["dp5_summary"] = summary
    statistics = (f" min={summary['min_score_percent']:.2f}%, max={summary['max_score_percent']:.2f}%, "
                  f"mean={summary['mean_score_percent']:.2f}%." if scores else "")
    _dispatch_log(event_dispatcher, f"DP5q enrichment complete: n={len(scores)}, skipped={summary['skipped']}, failed={failed}.{statistics}")
    if skipped:
        reasons = ", ".join(f"{reason}={count}" for reason, count in sorted(skipped.items()))
        _dispatch_log(event_dispatcher, f"DP5q skip reasons: {reasons}.")
    if skipped.get("structure_unavailable_deep_name_analysis_off"):
        _dispatch_log(event_dispatcher,
                      f"DP5q skipped {skipped['structure_unavailable_deep_name_analysis_off']} compound(s) because "
                      "deep name analysis is off and names are not resolved to structures; "
                      "turn deep name analysis on to resolve them with local OPSIN before scoring.",
                      ErrorSeverity.WARNING)
    return analysis_data


def _compound_request(cfg: AppConfig, compound: dict, index: int) -> CheckRequest | str:
    """Validate parser coverage before starting a child, retaining input precision."""
    smiles = compound.get("smiles")
    if not smiles:
        return "structure_unavailable_deep_name_analysis_off" if cfg.modes.deep_name_analysis == DeepNameAnalysis.OFF else "smiles_resolution_failed"
    if len(str(smiles)) > MAX_SMILES_LENGTH:
        return "structure_input_limit"
    mol = Chem.MolFromSmiles(str(smiles))
    if mol is None:
        return "invalid_structure"
    if has_boron(mol):
        raise DP5PreparationSkip(BORON_REASON, warning_code=ErrorCode.DP5_BORON_NOT_SUPPORTED)
    try:
        component = select_scoring_component(mol)
    except ComponentSelectionError as exc:
        raise DP5PreparationSkip(exc.reason, info_message=str(exc)) from exc
    if component.mol.GetNumAtoms() > MAX_ATOMS:
        raise DP5PreparationSkip("atom_limit", warning_code=ErrorCode.DP5_NOT_APPLICABLE,
                                 warning_details=f"DP5q supports at most {MAX_ATOMS} atoms; this structure exceeds that limit.")
    refusal_details = compound.get("dp5_refusal_details") or {}
    if refusal_details.get("cause") == "signal_limit_exceeded":
        return "carbon_signal_limit"
    shifts = compound.get("dp5_c_signals_list")
    if not shifts:
        return "carbon_nmr_missing"
    groups = compound.get("dp5_carbon_groups", [])
    if len(shifts) > MAX_GROUPS or (isinstance(groups, list) and len(groups) > MAX_GROUPS):
        raise DP5PreparationSkip("group_limit", warning_code=ErrorCode.DP5_NOT_APPLICABLE,
                                 warning_details=f"DP5q supports at most {MAX_GROUPS} observed carbon groups.")
    try:
        observations = compound.get("carbon_observations")
        if observations is not None and (
            not isinstance(observations, list) or len(observations) != len(shifts)
            or any(not isinstance(item, dict) or "represented_atoms" not in item or item.get("shift") != shift
                   for item, shift in zip(observations, shifts))
        ):
            raise DP5PreparationSkip("invalid_request", warning_details="Carbon observation evidence does not align with the prepared shifts.")
        observation_counts = ([item["represented_atoms"] for item in observations]
                              if observations is not None else None)
        if observation_counts is not None and not any(count is not None for count in observation_counts):
            observation_counts = None
        coalitions = compound.get('dp5_coincident_environments', ())
        run_dp5 = compound.get('run_dp5')
        if component.ignored_components and _required_carbon_shift_count(mol, groups) is not None:
            groups, coalitions = _component_request_groups(mol, component, groups, shifts, observation_counts,
                coalitions, compound.get('fixed_assignments', {}))
            # Whole-source count preparation used the disconnected legacy view.
            # Recheck it against the selected component, retaining parser errors.
            if refusal_details.get('cause') in {'count_mismatch', 'strict_nmr_coverage_mismatch',
                    'structure_partition_incomplete', 'annotation_inventory_mismatch',
                    'annotation_partition_mismatch', 'unresolved_coincident_environments'}:
                run_dp5 = True
                refusal_details = {}
        _check_nmr_coverage(shifts, mol, run_dp5, groups, refusal_details,
            observation_atom_counts=observation_counts,
            coincident_environments=coalitions,
            fixed_assignments=compound.get('fixed_assignments', {}))
        request = CheckRequest(compound_id=str(index), smiles=str(smiles), carbon_shifts=shifts,
                            carbon_groups=groups,
                            carbon_labels=tuple(f"C{atom.GetIdx()+1}" for atom in mol.GetAtoms() if atom.GetAtomicNum() == 6),
                            observation_atom_counts=observation_counts,
                            coincident_environments=coalitions,
                            fixed_assignments=compound.get("fixed_assignments", {}),
                            include_geometry=cfg.dp5.keep_artifacts)
        return request
    except AssignmentError as exc:
        raise DP5PreparationSkip(exc.reason, warning_details=f'{exc.reason}: {exc}') from exc
    except ValidationError as exc:
        raise DP5PreparationSkip("invalid_request", warning_details=str(exc)) from exc


def _component_request_groups(mol: Chem.Mol, component: ScoringComponent, groups, shifts,
                              observation_counts, coalitions, fixed_assignments):
    """Replace only the native disconnected producer's legacy partition."""
    projected = component.project_groups(groups)
    structure = analyze_molecular_structure_detailed(component.mol)
    try:
        policy = derive_observation_policy(component.mol, projected, structure=structure)
    except AssignmentError as exc:
        if exc.reason != 'source_partition_mismatch':
            raise DP5PreparationSkip(exc.reason, warning_details=f'{exc.reason}: {exc}') from exc
        legacy = analyze_molecular_structure_detailed(mol)
        if {frozenset(group) for group in groups} != {frozenset(group) for group in legacy.carbon_groups}:
            raise DP5PreparationSkip(exc.reason, warning_details=f'{exc.reason}: {exc}') from exc
        if fixed_assignments or coalitions:
            raise DP5PreparationSkip('source_partition_mismatch', warning_details=(
                'Explicit observation constraints refer to the disconnected legacy partition; '
                'they cannot be transferred to different component groups.')) from exc
        variants = ([variant.carbon_groups for variant in structure.isomer_partitions]
                    if structure.partition_complete else [structure.carbon_groups])
        policies = [derive_observation_policy(component.mol, candidate, structure=structure)
                    for candidate in variants]
        policy = next((candidate for candidate in policies if _carbon_coverage_error(shifts, len(candidate.groups),
            observation_atom_counts=observation_counts, coincident_environments=candidate.coincident_environments)
            is None), policies[0])
        groups = [list(component.original_labels(group.atom_indices)) for group in policy.groups
                  if len(group.atom_indices) > 1]
        coalitions = policy.coincident_environments
    return groups, coalitions or policy.coincident_environments


def _adapt_result(result: CheckResult) -> dict:
    """Keep report dictionary aliases while naming the scientific score correctly."""
    percent = result.score_raw * 100
    atoms = [{**row, "label": row["atom_label"], "assigned_shift": row["experimental_shift"],
              "error": row["scaled_error"], "atom_probability_raw": row["score_percent"] / 100}
             for row in result.assignments]
    diagnostics = {key: value for key, value in result.diagnostics.items() if key != "geometry_sdf"}
    return {"status": "success", "score_raw": result.score_raw, "score_percent": percent,
            "log_score": result.log_score, "probability_raw": result.score_raw,
            "probability_percent": percent, "atoms": atoms, "diagnostics": diagnostics,
            "provenance": result.provenance, "timings": result.timings}


def _check_nmr_coverage(
    carbon_shifts: list[float],
    mol: Chem.Mol,
    run_dp5: bool,
    carbon_groups: list[list[str]],
    refusal_details: dict | None = None,
    *, observation_atom_counts=None, coincident_environments=(), fixed_assignments=None,
) -> None:
    """Preserve the first coverage error and the active parser refusal's detail."""
    required_count = _required_carbon_shift_count(mol, carbon_groups)
    error = _carbon_coverage_error(carbon_shifts, required_count,
        observation_atom_counts=observation_atom_counts,
        coincident_environments=coincident_environments, fixed_assignments=fixed_assignments)
    if run_dp5 and error is None:
        return
    details = refusal_details or {}
    reason = details.get('cause') or (error.reason if error else 'preparation_refused')
    if error is not None and error.reason in {'invalid_request', 'invalid_equivalence_groups'}:
        reason = error.reason
    prepared_count = len(carbon_shifts) if isinstance(carbon_shifts, list) else 0
    if not run_dp5 and details.get('required_count') is not None:
        required_count = details['required_count']
    counts = observation_atom_counts
    counts_valid = counts is None or (len(counts) == prepared_count
        and all(count is None or type(count) is int and count > 0 for count in counts))
    represented_count = (sum(count if count is not None else 1 for count in counts)
                         if counts is not None and counts_valid else prepared_count)
    if (counts_valid and type(required_count) is int and represented_count != required_count
            and reason in {'count_mismatch', 'strict_nmr_coverage_mismatch', 'annotation_inventory_mismatch',
                           'annotation_partition_mismatch', 'unresolved_coincident_environments', 'malformed_separators'}):
        code = (reason if reason in {'annotation_inventory_mismatch', 'annotation_partition_mismatch',
                                     'unresolved_coincident_environments'} else 'strict_nmr_coverage_mismatch')
        raise DP5PreparationSkip(code, info_message=(
            f"DP5q not performed: the number of 13C signals ({represented_count}) "
            f"does not match the expected number ({required_count})."))
    causes = {
        'count_mismatch': 'the observed 13C signal count does not match the structure',
        'malformed_separators': 'the 13C signal separators are malformed',
        'incomplete_numeric_tokens': 'some numeric 13C signal tokens could not be prepared',
        'invalid_carbon_annotation': 'a carbon atom annotation is invalid',
        'annotation_inventory_mismatch': 'the represented carbon atom inventory is impossible',
        'annotation_partition_mismatch': 'carbon overlap annotations cannot fit the selected formal environments',
        'unresolved_coincident_environments': 'fewer observed entries than environments leave coincident signals unresolved',
        'source_partition_mismatch': 'supplied carbon groups differ from the source partition',
        'unsupported_coincidence_policy': 'the structural observation-sharing policy is unsupported',
        'assignment_search_limit': 'observation assignment exceeds the bounded computation allowance',
        'preparation_refused': '13C preparation supplied no accepted scoring request',
    }
    reason_details = details.get('reason_details') or {}
    cause = reason_details.get(reason)
    if reason == 'structure_partition_incomplete':
        cause = details.get('structure_reason') or cause
    if not cause and error is not None and error.reason == reason:
        cause = str(error)
    cause = cause or causes.get(reason) or reason.replace('_', ' ')
    raise DP5PreparationSkip(reason, warning_code=ErrorCode.DP5_NOT_APPLICABLE,
                             warning_details=f"{reason}: {cause.rstrip('. ')}.")


def _carbon_coverage_error(
    carbon_shifts: list[float], required_count: int | None,
    *, observation_atom_counts=None, coincident_environments=(), fixed_assignments=None,
) -> AssignmentError | None:
    """Return one actionable validation error instead of discarding its message."""
    if not isinstance(carbon_shifts, list) or not carbon_shifts:
        return AssignmentError('invalid_request', '13C shifts must be a nonempty list of finite numeric values in ppm.')
    try:
        numeric = all(type(shift) in (int, float) and math.isfinite(shift) for shift in carbon_shifts)
    except OverflowError:
        numeric = False
    if not numeric:
        return AssignmentError('invalid_request', '13C shifts must be finite numeric values in ppm.')
    if required_count is None:
        return AssignmentError('invalid_equivalence_groups', 'The supplied carbon equivalence groups are invalid.')
    try:
        validate_observation_coverage(required_count, len(carbon_shifts),
            observation_atom_counts=observation_atom_counts, coincident_environments=coincident_environments,
            fixed=fixed_assignments)
    except AssignmentError as exc:
        return exc
    return None


def _required_carbon_shift_count(mol: Chem.Mol, carbon_groups: list[list[str]]) -> int | None:
    """Return the strict shift count, or None for invalid equivalence groups."""
    if not isinstance(carbon_groups, list):
        return None

    carbon_labels = {
        f"C{atom.GetIdx() + 1}"
        for atom in mol.GetAtoms()
        if atom.GetSymbol() == "C"
    }
    grouped_labels = set()
    required_shifts = len(carbon_labels)
    for group in carbon_groups:
        if not isinstance(group, list) or len(group) < 2:
            return None
        for label in group:
            if not isinstance(label, str) or label not in carbon_labels or label in grouped_labels:
                return None
            grouped_labels.add(label)
        required_shifts -= len(group) - 1

    return required_shifts


def _refresh_highlight_color(compound: dict) -> None:
    warnings = compound.get("all_warnings", [])
    has_red = any(str(line).startswith("⚠️") for line in warnings)
    has_yellow = any(str(line).startswith("❗") for line in warnings)
    color = "red" if has_red else "yellow" if has_yellow else "blue"
    compound.setdefault("highlight_info", {})["color"] = color


def _merge_error_stats(target: dict, source: dict) -> None:
    for key, value in source.items():
        target[key] = int(target.get(key, 0)) + int(value)


def _dispatch_log(
    event_dispatcher: EventDispatcher | None,
    message: str,
    severity: ErrorSeverity = ErrorSeverity.INFO,
) -> None:
    if event_dispatcher:
        event_dispatcher(LogEvent(severity=severity, message=message))


def _dispatch_progress(
    event_dispatcher: EventDispatcher | None,
    current: int,
    total: int,
    message: str,
) -> None:
    if event_dispatcher:
        event_dispatcher(ProgressEvent(current=current, total=total, message=message))
