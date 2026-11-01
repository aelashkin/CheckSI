"""Internal reported HRMS mass parsing, comparisons and findings."""

import math
import re
from decimal import Decimal

from checksi.analysis.hrms_fields import find_hrms_formula
from checksi.analysis.mass_spec_utils import find_elemental_combinations_for_mass, format_combination
from checksi.config_schema import AppConfig, OutputMode
from checksi.errors import ErrorCode
from checksi.errors.manager import emit_finding
from checksi.errors.findings import AnalysisNote, messages, FindingSink, Diagnostic
from checksi.utils.error_handler import ErrorTracker


def _parse_mass_token(token: str) -> Decimal | None:
    """Accept a complete positive mass at the existing four-decimal precision."""
    match = re.fullmatch(r'\(?(\d+\.\d{4})[.,;:)\]]*', token)
    if not match:
        return None
    mass = Decimal(match.group(1))
    return mass if mass > 0 and math.isfinite(float(mass)) else None


def _found_mass(text: str) -> Decimal | None:
    """Read only an accepted optional label and its immediately following value."""
    text = text.lstrip()
    if text[:1] in (":", "=", ","):
        text = text[1:].lstrip()
    separator = re.match(r'[\-–—]\s+', text)
    if separator:
        text = text[separator.end():]
    label = re.match(r'm/z(?=\s|[:=]|$)', text, re.IGNORECASE)
    if label:
        text = text[label.end():].lstrip()
        if text[:1] in (":", "="):
            text = text[1:].lstrip()
    ion = re.match(r'\[(?:[^\[\]]|\[[^\[\]]*\])*\][+\-–—]?', text)
    if ion:
        text = text[ion.end():].lstrip()
    tokens = text.split(None, 1)
    return _parse_mass_token(tokens[0]) if tokens else None


def extract_reported_masses(text: str, *, _owned: bool = False) -> tuple[Decimal | None, Decimal | None]:
    """Read independent mass fields belonging to the identified HRMS formula."""
    formula_match = None if _owned else find_hrms_formula(text)
    if formula_match is not None:
        text = text[formula_match.header_start:]
    if formula_match is not None or _owned:
        next_section = re.search(r'Anal\.?\s*calcd\.?|\b(?:\d+[A-Za-z]+[\s-]*)?NMR\b', text, re.IGNORECASE)
        if next_section:
            text = text[:next_section.start()]
    found_marker = re.search(r'\bfound(?=[^A-Za-z_]|$)', text, re.IGNORECASE)
    calculated_text = text[:found_marker.start()] if found_marker else text
    if found_marker is None:
        observed = re.search(r'\b(?:observed|measured|obsd|experimental)\b|\bexp\.', calculated_text, re.IGNORECASE)
        if observed:
            calculated_text = calculated_text[:observed.start()]
    calculated_match = re.search(r'(?:^|[\s,:;=\])+])(\d+\.\d{4}[.,;:)\]]*)\s*$', calculated_text)
    calculated = _parse_mass_token(calculated_match.group(1)) if calculated_match else None
    if found_marker is None and len(re.findall(r'(?<![\w.])\d+\.\d{4}(?!\d)', calculated_text)) > 1:
        calculated = None
    found = _found_mass(text[found_marker.end():]) if found_marker else None
    return calculated, found


def mass_error_ppm(calculated: Decimal, found: Decimal) -> Decimal:
    """Calculate ppm from exact reported decimals, preserving the strict 10 ppm limit."""
    return abs(calculated - found) / calculated * Decimal(1_000_000)


def _compare_calculated_mass(
    cfg: AppConfig, tracker: ErrorTracker | FindingSink, output: list[Diagnostic],
    formula_mass: float, calculated: Decimal, charge_ion, charge_formula,
) -> None:
    """Typed implementation behind `compare_calculated_mass`."""
    if Decimal(str(round(formula_mass, 4))) == calculated:
        if cfg.modes.output_mode == OutputMode.DETAILED:
            output.append(AnalysisNote("calcd mass match formula"))
        return
    if cfg.modes.output_mode == OutputMode.DETAILED:
        output.append(AnalysisNote(f"mass according formula: {formula_mass:.4f}, charge in ion: {charge_ion}, charge in formula: {charge_formula}"))
    difference = abs(Decimal(str(formula_mass)) - calculated)
    if difference <= Decimal("0.0002"):
        warning = emit_finding(cfg, tracker, ErrorCode.CALCD_FORMULA_MASS_MISMATCH, variant="possible_typo")
        if warning:
            output.append(warning)
        return
    combinations = find_elemental_combinations_for_mass(float(difference), max_count=7, tolerance=0.0001)
    warning = emit_finding(cfg, tracker, ErrorCode.CALCD_FORMULA_MASS_MISMATCH)
    if warning:
        output.append(warning)
    context = {"difference": float(difference)}
    if combinations:
        context["combinations"] = "; ".join(format_combination(combo) for combo in combinations)
    delta_warning = emit_finding(
        cfg, tracker, ErrorCode.CALCD_FORMULA_MASS_DELTA, context=context,
        variant="with_combinations" if combinations else "without_combinations",
    )
    if delta_warning:
        output.append(delta_warning)


def _check_reported_masses(
    cfg: AppConfig, tracker: ErrorTracker | FindingSink, text, output, mass_from_formula_hrms,
    formula, charge_ion, charge_formula, *, _owned: bool = False,
):
    """Typed implementation behind `check_reported_masses`."""
    calculated, found = extract_reported_masses(text, _owned=_owned)
    if calculated is None:
        warning = emit_finding(cfg, tracker, ErrorCode.HRMS_CALCD_MASS_MISSING)
        if warning:
            output.append(warning)
    elif mass_from_formula_hrms is not None:
        _compare_calculated_mass(cfg, tracker, output, mass_from_formula_hrms, calculated, charge_ion, charge_formula)
    if found is None:
        warning = emit_finding(cfg, tracker, ErrorCode.HRMS_FOUND_MASS_MISSING)
        if warning:
            output.append(warning)
    elif calculated is not None:
        ppm_diff = mass_error_ppm(calculated, found)
        if cfg.modes.output_mode == OutputMode.DETAILED:
            output.append(AnalysisNote(f"mass data ppm: {ppm_diff:.2f} ppm"))
        if ppm_diff > 10:
            ppm_warning = emit_finding(cfg, tracker, ErrorCode.HRMS_PPM_TOO_LARGE, context={"ppm_diff": ppm_diff})
            ppm_values_warning = emit_finding(
                cfg, tracker, ErrorCode.HRMS_PPM_TOO_LARGE,
                context={"calcd_mass": float(calculated), "found_mass": float(found)}, variant="values",
            )
            if ppm_warning:
                output.append(ppm_warning)
            if ppm_values_warning:
                output.append(ppm_values_warning)
    if cfg.modes.output_mode == OutputMode.DETAILED:
        if calculated is not None:
            output.append(AnalysisNote(f"calcd mass: {float(calculated)}"))
        if found is not None:
            output.append(AnalysisNote(f"found mass: {float(found)}"))
        if mass_from_formula_hrms is not None and formula:
            output.append(AnalysisNote(f"HRMS mass (from formula): {mass_from_formula_hrms:.4f} ({formula})"))
    return output


def compare_calculated_mass(
    cfg: AppConfig, tracker: ErrorTracker, output: list[str],
    formula_mass: float, calculated: Decimal, charge_ion, charge_formula,
) -> None:
    """Emit existing formula-mass findings with exact decimal threshold comparisons."""
    diagnostics = []
    _compare_calculated_mass(cfg, tracker, diagnostics, formula_mass, calculated, charge_ion, charge_formula)
    output.extend(messages(diagnostics))


def check_reported_masses(
    cfg: AppConfig, tracker: ErrorTracker, text, output, mass_from_formula_hrms,
    formula, charge_ion, charge_formula, *, _owned: bool = False,
):
    """Validate each reported field before formula and ppm comparisons."""
    diagnostics = []
    _check_reported_masses(cfg, tracker, text, diagnostics, mass_from_formula_hrms,
                           formula, charge_ion, charge_formula, _owned=_owned)
    output.extend(messages(diagnostics))
    return output
