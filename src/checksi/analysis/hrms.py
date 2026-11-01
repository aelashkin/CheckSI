"""Compatible HRMS entry points for formula, charge and measurement checks."""

from checksi.config_schema import AppConfig, OutputMode
from checksi.errors import ErrorCode
from checksi.errors.manager import emit_finding
from checksi.errors.findings import AnalysisNote, messages, FindingSink
from checksi.analysis.formula_utils import _formula_refusal_lines
from checksi.analysis.formula_utils import _get_formula_order_findings
from checksi.analysis.formula_utils import _get_isotope_specification_warnings
from checksi.analysis.formula_utils import _validate_formula_and_get_mass
from checksi.analysis.hrms_ions import _adjust_ion_composition, charge_evidence
from checksi.analysis.hrms_measurements import _check_reported_masses
# Compatibility re-exports; typed HRMS producers use the private helpers above.
from checksi.analysis.formula_utils import (
    formula_refusal_lines as formula_refusal_lines,
    replace_common_isotope_formats,
    validate_formula_and_get_mass as validate_formula_and_get_mass,
    get_formula_order_findings as get_formula_order_findings,
    get_isotope_specification_warnings as get_isotope_specification_warnings,
)
from checksi.analysis.hrms_fields import (
    NOTATION_NOT_SUPPORTED, find_hrms_formula, find_hrms_formula_candidate,
)
from checksi.analysis.hrms_ions import adjust_ion_composition as adjust_ion_composition
from checksi.analysis.hrms_measurements import check_reported_masses as check_reported_masses
from checksi.analysis.mass_spec_utils import ELECTRON_MASS
from checksi.utils.error_handler import ErrorTracker


def _adjust_mass_by_hrms(cfg: AppConfig, tracker: ErrorTracker | FindingSink, text, output, formula, real_mass, *, details: dict | None = None, original_text: str | None = None):
    """Typed implementation behind `adjust_mass_by_hrms`."""
    return _adjust_ion_composition(cfg, tracker, text, output, formula, real_mass, details=details, original_text=original_text)


def _extract_formula_token(text: str) -> str | None:
    """Keep the compatible token helper backed by shared source identification."""
    match = find_hrms_formula(text)
    return match.formula if match is not None else None


def _check_hrms(cfg: AppConfig, tracker: ErrorTracker | FindingSink, text, output, *, details: dict | None = None):
    """Typed implementation behind `check_hrms`."""

    # Search for the formula in HRMS
    candidate = find_hrms_formula_candidate(text)
    if details is not None:
        details.clear()
        details.update(source='hrms', span_basis='input_text',
                       candidate=candidate.to_dict() if candidate else None,
                       status='absent' if candidate is None else 'unsupported',
                       usable=False, formula=None, mass=None)
    if candidate is not None and candidate.formula is None:
        output.extend(_formula_refusal_lines(cfg, tracker, candidate, 'HRMS'))
        if details is not None and candidate.refusal != NOTATION_NOT_SUPPORTED:
            details['status'] = 'invalid'
        return output, None, None
    formula_match = candidate
    formula_parsed = candidate.formula if candidate is not None else None
    charge_formula = None
    formula = None
    charge_ion = None
    pre_real_mass = 0
    mass_from_formula_hrms = None
    evidence = None
    formula_atom_symbol_warning = None
    if formula_parsed:
        # Bring isotopes to a readable format
        formula = replace_common_isotope_formats(formula_parsed)
       
        

        # Check that isotopes for chlorine and bromine are specified
        isotope_warnings = _get_isotope_specification_warnings(cfg, tracker, formula)
        if isotope_warnings:
            output.extend(isotope_warnings)

        if formula:
            # Check the correctness of isotope notation, determine the molecular mass, check the correctness of atoms
            mass_from_formula_hrms, formula_atom_symbol_warning, formula_isotope_format_warnings = (
                _validate_formula_and_get_mass(cfg, tracker, formula)
            )
            pre_real_mass = mass_from_formula_hrms
            if formula_isotope_format_warnings:
                for warning in formula_isotope_format_warnings:
                    output.append(warning)
            if mass_from_formula_hrms is None:
                formula = None
                if formula_atom_symbol_warning:
                    output.append(formula_atom_symbol_warning)
            else:
                output.extend(_get_formula_order_findings(cfg, tracker, formula))
                
                evidence = charge_evidence(text, candidate)
                def label(value):
                    if value is None:
                        return None
                    return ('+' if value > 0 else '-') if abs(value) == 1 else f"{value:+d}"
                charge_formula, charge_ion = label(evidence['formula']), label(evidence['ion'])
                if evidence['formula_charge_missing']:
                    warning = emit_finding(cfg, tracker, ErrorCode.HRMS_FORMULA_CHARGE_MISSING)
                    if warning:
                        output.append(warning)
                if evidence['ion_charge_missing']:
                    warning = emit_finding(cfg, tracker, ErrorCode.HRMS_ION_CHARGE_MISSING)
                    if warning:
                        output.append(warning)
                if evidence['conflict']:
                    ion_assertion = '/'.join(label(value) for value in evidence['ion_assertions'])
                    warning = emit_finding(cfg, tracker, ErrorCode.HRMS_CHARGE_INCONSISTENT,
                        context={"charge_ion": ion_assertion or charge_ion or 'unspecified',
                                 "charge_formula": charge_formula or 'unspecified'})
                    if warning:
                        output.append(warning)
                charge = evidence['effective']
                if charge is not None:
                    mass_from_formula_hrms = round(
                        (mass_from_formula_hrms - charge * ELECTRON_MASS) / abs(charge), 4)
                if cfg.modes.output_mode == OutputMode.DETAILED and charge_formula == charge_ion and charge_formula:
                    output.append(AnalysisNote(f"the charges match. charges: {charge_ion}, {charge_formula}"))

        _check_reported_masses(
            cfg, tracker, text[formula_match.header_start:], output, mass_from_formula_hrms,
            formula, charge_ion, charge_formula, _owned=True,
        )
    if details is not None and candidate is not None:
        details.update(status='valid' if formula else 'invalid', usable=bool(formula),
                       formula=formula, mass=pre_real_mass,
                       charge_formula=charge_formula, charge_ion=charge_ion,
                       charge=evidence['effective'] if evidence else None)
    return output, formula, pre_real_mass


def adjust_mass_by_hrms(cfg: AppConfig, tracker: ErrorTracker, text, output, formula, real_mass, *, details: dict | None = None):
    """Recover the neutral composition from an HRMS ion description."""
    diagnostics = []
    _, first, second = _adjust_mass_by_hrms(cfg, tracker, text, diagnostics, formula, real_mass, details=details)
    output.extend(messages(diagnostics))
    return output, first, second


def check_hrms(cfg: AppConfig, tracker: ErrorTracker, text, output, *, details: dict | None = None):
    """Validate the identified ion formula and its reported mass measurements."""
    diagnostics = []
    _, first, second = _check_hrms(cfg, tracker, text, diagnostics, details=details)
    output.extend(messages(diagnostics))
    return output, first, second
