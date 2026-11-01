# Project imports. Public value/formula helpers remain compatibility re-exports;
# internal analysis uses their typed private counterparts below.
from checksi.analysis._elemental_values import (
    calculate_elemental_percentages,
    compare_formula_composition as compare_formula_composition,
    compare_reported_compositions as compare_reported_compositions,
    extract_composition_sections,
    parse_composition_values as parse_composition_values,
)
from checksi.analysis.formula_utils import (
    formula_refusal_lines as formula_refusal_lines,
    get_formula_order_findings as get_formula_order_findings,
    replace_common_isotope_formats,
    validate_formula_and_get_mass as validate_formula_and_get_mass,
)
from checksi.analysis.hrms_fields import (
    NOTATION_NOT_SUPPORTED, find_ea_formula_candidate, find_hrms_formula_candidate,
)
from checksi.analysis.formula_source_policy import (
    ea_atom_count_percentages, ea_molar_mass, ea_percentages, integer_formula, parse_ea_formula,
)
from molmass import Formula
from checksi.config_schema import AppConfig, OutputMode
from checksi.errors import ErrorCode
from checksi.errors.manager import emit_finding
from checksi.errors.findings import AnalysisNote, messages, FindingSink
from checksi.analysis._elemental_values import _compare_formula_composition
from checksi.analysis._elemental_values import _compare_reported_compositions
from checksi.analysis._elemental_values import _parse_composition_values
from checksi.analysis.formula_utils import _formula_refusal_lines
from checksi.analysis.formula_utils import _get_formula_order_findings
from checksi.analysis.formula_utils import _validate_formula_and_get_mass
from checksi.utils.error_handler import ErrorTracker


def _validate_elemental_analysis_values(
    config: AppConfig,
    tracker: ErrorTracker | FindingSink,
    text_block,
    ea_formula_string,
):
    """Typed implementation behind `validate_elemental_analysis_values`."""
    theoretical = calculate_elemental_percentages(ea_formula_string)
    calculated_section, found_section = extract_composition_sections(text_block)
    calculated = _parse_composition_values(config, tracker, calculated_section, "calculated")
    found = _parse_composition_values(config, tracker, found_section, "found")
    matches, comparison_warnings, tolerance_errors = _compare_reported_compositions(
        config, tracker, calculated.formatted, found.formatted,
    )
    policy = parse_ea_formula(ea_formula_string)
    isotope_percentages_pending = any(key[0].isdigit() for key in policy.full_counts)
    alternative = ({element: round(percent, 2) for element, percent in
                    ea_atom_count_percentages(policy.full_counts).items()} if isotope_percentages_pending else None)
    formula_warnings = _compare_formula_composition(config, tracker, theoretical, calculated.numeric,
                                                   alternative=alternative)
    analysis_report = {
        'calculated': calculated.formatted,
        'found': found.formatted,
        'matches': matches,
        'warnings': calculated.warnings + found.warnings + comparison_warnings + formula_warnings,
        'format_errors': calculated.format_errors + found.format_errors,
        'tolerance_errors': tolerance_errors,
    }
    detailed_log = [
        AnalysisNote(f"Theoretical elemental analysis from formula: {theoretical}"),
        AnalysisNote(f"Calculated elemental analysis from text: {calculated.numeric}"),
        AnalysisNote(f"Found elemental analysis from text: {found.numeric}"),
    ]
    return analysis_report, detailed_log


def _check_elemental_analysis(
    config: AppConfig,
    tracker: ErrorTracker | FindingSink,
    text_block,
    report_lines,
    *,
    details: dict | None = None,
):
    formula_atom_symbol_warning = None
    ea_formula_string = None
    mass_from_ea_formula = 0
    
    # Search for a formula in the HRMS section to check for its existence later
    hrms_formula_match = find_hrms_formula_candidate(text_block)
    
    # Search for the formula specifically in the Elemental Analysis section
    ea_formula_match = find_ea_formula_candidate(text_block)
    if details is not None:
        details.clear()
        details.update(source='ea', span_basis='input_text',
                       candidate=ea_formula_match.to_dict() if ea_formula_match else None,
                       status='absent' if ea_formula_match is None else 'unsupported',
                       usable=False, formula=None, mass=None)
    if ea_formula_match is not None and ea_formula_match.formula is None:
        report_lines.extend(_formula_refusal_lines(config, tracker, ea_formula_match, 'EA'))
        if details is not None and ea_formula_match.refusal != NOTATION_NOT_SUPPORTED:
            details['status'] = 'invalid'
        return report_lines, None, None

    if ea_formula_match is not None:
        policy = parse_ea_formula(ea_formula_match.formula)
        if details is not None and policy.status == 'accepted' and any(key[0].isdigit() for key in policy.full_counts):
            details['percentage_conventions'] = {
                'interpretation': 'pending', 'molar_mass_basis': 'actual fixed-isotope inventory',
                'molar_mass': ea_molar_mass(policy.full_counts),
                'mass_based': {key: round(value, 2) for key, value in ea_percentages(policy.full_counts).items()},
                'atom_count_based': {key: round(value, 2) for key, value in ea_atom_count_percentages(policy.full_counts).items()},
            }
        special = policy.status == 'accepted' and (
            policy.explicit_components or any(count.denominator != 1 for count in policy.full_counts.values()))
        if special:
            parent = policy.comparable_parent
            parent_formula = integer_formula(parent) if parent else None
            parent_mass = Formula(parent_formula).isotope.mass if parent_formula else None
            full_formula = integer_formula(policy.full_counts)
            full_mass = Formula(full_formula).isotope.mass if full_formula else None
            _append_elemental_values(config, tracker, text_block, ea_formula_match.formula, report_lines)
            if parent_formula is None:
                report_lines.append(AnalysisNote(
                    'EA percentages use the full fractional formula; the formula alone admits several integer-parent solvent explanations.'))
            if details is not None:
                details.update(status='valid', usable=True, formula=ea_formula_match.formula, mass=full_mass,
                               comparison_formula=parent_formula, comparison_mass=parent_mass)
            return report_lines, parent_formula, parent_mass
    
    if ea_formula_match:
        ea_formula_string = ea_formula_match.formula
        if ea_formula_string:
            # Convert isotopes into a readable format for parsing
            ea_formula_string = replace_common_isotope_formats(ea_formula_string)

            # Validate the formula's notation, atoms, and calculate its mass
            mass_from_ea_formula, formula_atom_symbol_warning, isotope_format_warnings = (
                _validate_formula_and_get_mass(config, tracker, ea_formula_string)
            )

            if mass_from_ea_formula is not None:
                # Check the order of elements in the formula only after syntax validation.
                report_lines.extend(_get_formula_order_findings(config, tracker, ea_formula_string))
            
            if isotope_format_warnings:
                for warning_message in isotope_format_warnings:
                    report_lines.append(warning_message)
            
            if config.modes.output_mode == OutputMode.DETAILED:
                report_lines.append(AnalysisNote(f"Elemental analysis formula: {ea_formula_string}"))

            if mass_from_ea_formula is None:
                # If the formula has an invalid atom, nullify it and report the error
                ea_formula_string = None
                if formula_atom_symbol_warning:
                    report_lines.append(formula_atom_symbol_warning)
            else:
                # If the formula is valid, proceed to check the elemental analysis data
                ea_check_results, detailed_analysis_log = _validate_elemental_analysis_values(
                    config,
                    tracker,
                    text_block,
                    ea_formula_string,
                )
        
                if detailed_analysis_log and config.modes.output_mode == OutputMode.DETAILED:
                    for log_line in detailed_analysis_log:
                        report_lines.append(log_line)

                if ea_check_results:
                    for key, value in ea_check_results.items():
                        if not value:
                            continue  # Skip empty result categories
                        if isinstance(value, list):
                            if key == 'matches':
                                if config.modes.output_mode == OutputMode.DETAILED:
                                    match_summary_string = ', '.join(f"{item}" for item in value)
                                    report_lines.append(AnalysisNote(f"Found vs calculated (EA): {match_summary_string}"))
                            else:
                                for result_line in value:
                                    if result_line is not None:
                                        report_lines.append(result_line)
    else:
        # This block is entered if no Elemental Analysis section is found.
        # Check if an HRMS section was found either.
        if not hrms_formula_match:
            # If there is neither HRMS nor Elemental analysis, add a warning
            warning = emit_finding(config, tracker, ErrorCode.FORMULA_NOT_FOUND)
            if warning:
                report_lines.append(warning)
   
    if config.modes.output_mode == OutputMode.DETAILED:
        if not hrms_formula_match:
            report_lines.append(AnalysisNote("molecular formula not found or no HRMS data"))
       
    if details is not None and ea_formula_match is not None:
        details.update(status='valid' if ea_formula_string else 'invalid',
                       usable=bool(ea_formula_string), formula=ea_formula_string,
                       mass=mass_from_ea_formula)
    return report_lines, ea_formula_string, mass_from_ea_formula


def _append_elemental_values(config, tracker, text_block, formula, report_lines):
    """Append the same percentage checks for complete integer and solvated formulas."""
    results, logs = _validate_elemental_analysis_values(config, tracker, text_block, formula)
    if config.modes.output_mode == OutputMode.DETAILED:
        report_lines.extend(logs)
    for key, values in results.items():
        if not isinstance(values, list) or not values:
            continue
        if key == 'matches':
            if config.modes.output_mode == OutputMode.DETAILED:
                report_lines.append(AnalysisNote('Found vs calculated (EA): ' + ', '.join(map(str, values))))
        else:
            report_lines.extend(value for value in values if value is not None)


def validate_elemental_analysis_values(
    config: AppConfig,
    tracker: ErrorTracker,
    text_block,
    ea_formula_string,
):
    """Validate extracted EA values through the shared internal chemistry stages."""
    result, log = _validate_elemental_analysis_values(config, tracker, text_block, ea_formula_string)
    return {**result, **{key: messages(result[key]) for key in ("warnings", "format_errors", "tolerance_errors")}}, messages(log)


def check_elemental_analysis(
    config: AppConfig,
    tracker: ErrorTracker,
    text_block,
    report_lines,
    *,
    details: dict | None = None,
):
    """Extract and validate elemental-analysis formula and values, returning warnings, formula and mass."""
    diagnostics = []
    _, first, second = _check_elemental_analysis(config, tracker, text_block, diagnostics, details=details)
    report_lines.extend(messages(diagnostics))
    return report_lines, first, second
