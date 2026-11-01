# Standard library imports
import math
import re

# Third-party imports
from periodictable import formula as periodictable_formula
from molmass import Formula, FormulaError

# Project imports
from checksi.analysis.formula_isotopes import (
    ISOTOPIC_ELEMENTS as ISOTOPIC_ELEMENTS,
    _count_isotopic_elements as _count_isotopic_elements,
    _generate_isotopic_formula_combinations as _generate_isotopic_formula_combinations,
    get_isotopic_masses as get_isotopic_masses,
    replace_common_isotope_formats as replace_common_isotope_formats,
)
from checksi.analysis.hrms_fields import NOTATION_NOT_SUPPORTED, FormulaCandidate
from checksi.config_schema import AppConfig
from checksi.errors import ErrorCode
from checksi.errors.manager import emit_finding
from checksi.errors.findings import AnalysisNote, message, messages, FindingSink, Diagnostic
from checksi.utils.error_handler import ErrorTracker


def _formula_refusal_lines(
    config: AppConfig,
    tracker: ErrorTracker | FindingSink,
    candidate: FormulaCandidate,
    source: str,
) -> list[Diagnostic]:
    """Typed implementation behind `formula_refusal_lines`."""
    if candidate.refusal == NOTATION_NOT_SUPPORTED:
        # Coverage records this omission; it is not a finding about the formula.
        return [AnalysisNote(f"Formula not checked by CheckSI: the {source} token '{candidate.raw}' "
                             "uses notation outside the supported formula grammar.")]
    warning = emit_finding(
        config, tracker, ErrorCode.FORMULA_INVALID_FORMAT, variant=candidate.refusal,
        context={'source': source, 'molecular_formula': candidate.raw, 'detail': candidate.refusal_detail},
    )
    return [warning] if warning else []


def get_iupac_formula_order_warnings(
    config: AppConfig,
    tracker: ErrorTracker,
    molecular_formula: str,
) -> str:
    """Return formula-order findings as the compatible formatted string."""
    return "\n".join(get_formula_order_findings(config, tracker, molecular_formula))


def _get_formula_order_findings(
    config: AppConfig,
    tracker: ErrorTracker | FindingSink,
    molecular_formula: str,
) -> list[Diagnostic]:
    """Typed implementation behind `get_formula_order_findings`."""
    findings = []
    matches = re.findall(r'\[(\d+[A-Z][a-z]?)\]|([A-Z][a-z]?)', molecular_formula)
    elements = [isotope or element for isotope, element in matches]
    seen = set()
    duplicates = sorted({element for element in elements if element in seen or seen.add(element)})
    if duplicates:
        warning = emit_finding(
            config, tracker, ErrorCode.FORMULA_DUPLICATE_ELEMENTS,
            context={"duplicates": ", ".join(duplicates), "molecular_formula": molecular_formula},
        )
        if warning:
            findings.append(warning)

    clean_formula = molecular_formula.replace('[', '').replace(']', '')
    try:
        parsed_formula = periodictable_formula(clean_formula)
    except Exception:
        # This leaf boundary translates parser failures from periodictable's
        # implementation without importing its undeclared parser dependency.
        warning = emit_finding(
            config, tracker, ErrorCode.FORMULA_INVALID_FORMAT,
            context={"molecular_formula": molecular_formula},
        )
        if warning:
            findings.append(warning)
        return findings

    found = [element.symbol for element in parsed_formula.atoms]
    expected = [element for element in ('C', 'H') if element in found]
    expected.extend(sorted(element for element in found if element not in {'C', 'H'}))
    if found != expected:
        warning = emit_finding(
            config, tracker, ErrorCode.FORMULA_ORDER_INVALID,
            context={"molecular_formula": molecular_formula,
                     "found": ", ".join(found), "expected": ", ".join(expected)},
        )
        if warning:
            findings.append(warning)
    return findings


def _get_isotope_specification_warnings(
    config: AppConfig,
    tracker: ErrorTracker | FindingSink,
    formula_string,
):
    """Typed implementation behind `get_isotope_specification_warnings`."""
    warnings = []
    if 'Cl' in formula_string and not re.search(r'\[(35|37)Cl\]', formula_string):
        warning = emit_finding(
            config,
            tracker,
            ErrorCode.HALOGEN_ISOTOPE_UNSPECIFIED,
            variant="chlorine",
        )
        if warning:
            warnings.append(warning)
    if 'Br' in formula_string and not re.search(r'\[(79|81)Br\]', formula_string):
        warning = emit_finding(
            config,
            tracker,
            ErrorCode.HALOGEN_ISOTOPE_UNSPECIFIED,
            variant="bromine",
        )
        if warning:
            warnings.append(warning)
    return warnings


def _validate_formula_and_get_mass(
    config: AppConfig,
    tracker: ErrorTracker | FindingSink,
    formula_string,
):
    """Typed implementation behind `validate_formula_and_get_mass`."""
    format_warnings = []
    try:
        # Check for common incorrect formats like "Cl35" instead of "[35Cl]"
        isotope_notation_map = {"Cl35": "35Cl", "Cl37": "37Cl", "Br79": "79Br", "Br81": "81Br"}
        for incorrect, correct in isotope_notation_map.items():
            if incorrect in formula_string:
                warning = emit_finding(
                    config,
                    tracker,
                    ErrorCode.ISOTOPE_FORMAT_INVALID,
                    context={"incorrect": incorrect, "correct": correct},
                )
                if warning:
                    format_warnings.append(warning)
        
        molmass_formula = Formula(formula_string)
        monoisotopic_mass = molmass_formula.isotope.mass
        if not math.isfinite(monoisotopic_mass):
            raise ValueError("Formula mass is outside numeric capacity.")
        return monoisotopic_mass, None, format_warnings
    except (FormulaError, ValueError, OverflowError):
        # Catches errors from invalid atom symbols (e.g., "Cx")
        atom_symbol_warning = emit_finding(
            config,
            tracker,
            ErrorCode.FORMULA_ATOM_SYMBOL_INVALID,
            context={"formula_string": formula_string},
        )
        return None, atom_symbol_warning, format_warnings


def count_hydrogen(molecular_formula, log=None):
    _log = log or print
    try:
        formula = Formula(molecular_formula)  
        composition = formula.composition()  
        return composition["H"].count if "H" in composition else 0  
    except FormulaError as e:
        _log(str(e))
        
def count_carbon(molecular_formula, log=None):
    _log = log or print
    try:
        formula = Formula(molecular_formula)  
        composition = formula.composition()  
        return sum(entry.count for element, entry in composition.items() if re.fullmatch(r"(?:\d+)?C", element))
    except FormulaError as e:
        _log(str(e))


def formula_refusal_lines(
    config: AppConfig,
    tracker: ErrorTracker,
    candidate: FormulaCandidate,
    source: str,
) -> list[str]:
    """Report a definite formula defect, or note that the notation was not checked."""
    return messages(_formula_refusal_lines(config, tracker, candidate, source))


def get_formula_order_findings(
    config: AppConfig,
    tracker: ErrorTracker,
    molecular_formula: str,
) -> list[str]:
    """Check duplicate symbols, syntax and order independently of visibility."""
    return messages(_get_formula_order_findings(config, tracker, molecular_formula))


def get_isotope_specification_warnings(
    config: AppConfig,
    tracker: ErrorTracker,
    formula_string,
):
    """Checks if a formula with Cl or Br specifies the exact isotope."""
    return messages(_get_isotope_specification_warnings(config, tracker, formula_string))


def validate_formula_and_get_mass(
    config: AppConfig,
    tracker: ErrorTracker,
    formula_string,
):
    """
    Validates formula syntax, checks for common format errors, and calculates monoisotopic mass.

    Args:
        config: The application configuration object.
        formula_string: The formula to validate.

    Returns:
        A tuple of (mass, atom_symbol_warning, format_warnings_list).
        Returns mass as None on failure.
    """
    mass, warning, diagnostics = _validate_formula_and_get_mass(config, tracker, formula_string)
    return mass, message(warning), messages(diagnostics)
