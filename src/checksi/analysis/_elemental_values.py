"""Internal extraction, calculation and comparison stages for elemental analysis."""

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from molmass import Formula, FormulaError
from checksi.analysis.hrms_fields import find_ea_formula_candidate
from checksi.analysis.formula_source_policy import ea_percentages, parse_ea_formula

from checksi.config_schema import AppConfig, ErrorSeverity
from checksi.errors import ErrorCode
from checksi.errors.manager import emit_finding
from checksi.errors.findings import Diagnostic, messages, FindingSink
from checksi.errors.manager import get_error_severity
from checksi.utils.error_handler import ErrorTracker


# Preserve complete percentages before a sentence or a glued property/prose tail.
_COMPOSITION_SENTENCE_END = re.compile(r'\.(?=\s|$)(?!\s*;)|(?<=\d\.\d{2})\.(?=[A-Za-z(])')
_COMPOSITION_ENTRY = re.compile(r'\s*(\d*[A-Za-z]{1,2})(?:\s*,\s*|\s+|$)([^\s;]*)')
_ENTRY_START = re.compile(r'(?<!\w)(\d*[A-Za-z]{1,2})(?:\s*,\s*|\s+)([^\s;]*)')
_CALCULATED_ANNOTATION = re.compile(r'^\s*\((?:M\.W\.|cf\.\s*ref\.)[^)]*\)\s*:?\s*', re.IGNORECASE)
_PROPERTY_TAIL = re.compile(r'\s*(?:mp\b|IR\b|MS\b|ee\b|Rf(?=\s*=)|Et(?=\s+\d+(?:\.\d+)?\s+g\b))')
# Stop at the NMR headers supported by the analysis layer or another HRMS/EA section.
_NEXT_ANALYSIS_SECTION = re.compile(
    r'\b(?:'
    r'(?:1H|19F|11B|31P|29Si|2H|15N)\s*(?:\{1H\})?[\s-]?NMR\b'
    r'|13C\s*(?:\{(?:1H(?:,\s*19F)?|19F(?:,\s*1H)?)\}\s*)?NMR\b'
    r'|(?i:HRMS|APCI|ESI|MALDI|TOF)\b'
    r'|(?i:Anal\.?\s*Calcd\.?)(?=\s|:|$)'
    r')'
)


@dataclass
class CompositionValues:
    """Numeric text values, precision-checked values, and their findings."""

    numeric: dict[str, float] = field(default_factory=dict)
    formatted: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    format_errors: list[str] = field(default_factory=list)


@dataclass
class _CompositionValues:
    numeric: dict[str, float] = field(default_factory=dict)
    formatted: dict[str, float] = field(default_factory=dict)
    warnings: list[Diagnostic] = field(default_factory=list)
    format_errors: list[Diagnostic] = field(default_factory=list)


def calculate_elemental_percentages(formula_string: str) -> dict[str, float]:
    """Calculate rounded mass percentages using average atomic weights."""
    policy = parse_ea_formula(formula_string)
    if policy.status == 'accepted':
        return {element: round(percent, 2) for element, percent in ea_percentages(policy.full_counts).items()}
    dataframe = Formula(formula_string).composition().dataframe()
    return {element: round(row['Fraction'] * 100, 2) for element, row in dataframe.iterrows()}


def _is_element_label(label: str) -> bool:
    """Distinguish actual element labels from property abbreviations."""
    symbol = label.lstrip('0123456789')
    if symbol in {'D', 'T'}:
        return True
    try:
        return Formula(symbol).formula == symbol
    except FormulaError:
        return False


def _before_composition_sentence_end(section: str) -> str:
    """Apply sentence boundaries after fields, retaining header residue for validation."""
    first_entry = next((match for match in _ENTRY_START.finditer(section) if _is_element_label(match.group(1))), None)
    boundary = _COMPOSITION_SENTENCE_END.search(section, first_entry.start() if first_entry else 0)
    return (section[:boundary.start()] if boundary else section).strip().rstrip(';')


def extract_composition_sections(text_block: str) -> tuple[str | None, str | None]:
    """Extract the existing calculated and found EA value sections."""
    candidate = find_ea_formula_candidate(text_block)
    if candidate is not None and candidate.formula is not None:
        # Formula dots and fractional counts are not percentage fields. Start
        # after the complete owned token rather than re-reading a short prefix.
        tail = text_block[candidate.charge_end or candidate.end:]
        tail = _NEXT_ANALYSIS_SECTION.split(tail, maxsplit=1)[0]
        found_marker = re.search(r'\bFound\s*:?\s*', tail, re.IGNORECASE)
        calculated = tail[:found_marker.start()] if found_marker else tail
        calculated = re.sub(r'^\s*:\s*', '', calculated, count=1)
        calculated = _CALCULATED_ANNOTATION.sub('', calculated, count=1)
        calculated = re.sub(r'^\s*\.\s+', '', calculated, count=1)
        found = tail[found_marker.end():] if found_marker else None
        return _before_composition_sentence_end(calculated), (
            _before_composition_sentence_end(found) if found is not None else None)
    calculated_match = re.search(
        r'Anal[\.]?\s*Calcd[\.]?\s*(?:for|:)?\s*[\w\(\)\[\]\+\-]*\s*:?\s*(.*?)(?=\s*Found\s*:?|$)',
        text_block,
        re.IGNORECASE,
    )
    calculated = None
    found_text = text_block
    if calculated_match:
        calculated_sections = _NEXT_ANALYSIS_SECTION.split(calculated_match.group(1), maxsplit=1)
        calculated_values = _CALCULATED_ANNOTATION.sub('', calculated_sections[0], count=1)
        calculated_values = re.sub(r'^\s*\.\s+', '', calculated_values, count=1)
        calculated = _before_composition_sentence_end(calculated_values)
        # A Found header in a later analysis section does not belong to this EA.
        found_text = text_block[calculated_match.end():] if len(calculated_sections) == 1 else ""
    found_match = re.search(r'Found\s*:?\s*', found_text, re.IGNORECASE)
    found = None
    if found_match:
        # Keep malformed/missing values for validation instead of stopping at them.
        found_section = _NEXT_ANALYSIS_SECTION.split(found_text[found_match.end():], maxsplit=1)[0]
        found = _before_composition_sentence_end(found_section)
    return calculated, found


def extract_composition_entries(section: str | None) -> list[tuple[str | None, str]]:
    """Split records, retaining malformed chunks with an explicit absent element."""
    entries = []
    if section:
        for pair in filter(None, re.split(r';\s*', section)):
            if _PROPERTY_TAIL.match(pair):
                break
            match = _COMPOSITION_ENTRY.match(pair)
            if match and _is_element_label(match.group(1)):
                entries.append((match.group(1), match.group(2).strip(',')))
            elif pair.strip():
                entries.append((None, pair.strip()))
    return entries


def is_two_decimal_percentage(percentage_string: str) -> bool:
    """Check the established unsigned two-decimal percentage notation."""
    return re.fullmatch(r'\d+\.\d{2}', percentage_string) is not None


def _parse_composition_values(
    config: AppConfig,
    tracker: ErrorTracker | FindingSink,
    section: str | None,
    context: str,
) -> _CompositionValues:
    """Typed implementation behind `parse_composition_values`."""
    values = _CompositionValues()
    entries = extract_composition_entries(section)
    element_counts = Counter(element for element, _ in entries if element is not None)
    reported_duplicates = set()
    for element, percentage_string in entries:
        if element is None:
            warning = emit_finding(
                config,
                tracker,
                ErrorCode.EA_NUMBER_FORMAT_INVALID,
                context={"context": f"{context} composition", "entry": percentage_string},
                variant="malformed_entry",
            )
            if warning:
                values.format_errors.append(warning)
            continue
        if element_counts[element] > 1:
            if element not in reported_duplicates:
                reported_duplicates.add(element)
                warning = emit_finding(
                    config,
                    tracker,
                    ErrorCode.EA_NUMBER_FORMAT_INVALID,
                    context={"atom_symbol": element, "context": f"{context} composition"},
                    variant="duplicate_element",
                )
                if warning:
                    values.format_errors.append(warning)
            continue
        if not percentage_string:
            warning = emit_finding(
                config,
                tracker,
                ErrorCode.EA_NUMBER_FORMAT_INVALID,
                context={"atom_symbol": element, "context": f"{context} composition"},
                variant="missing_value",
            )
            if warning:
                values.format_errors.append(warning)
            continue
        numeric_value = None
        try:
            parsed_value = float(percentage_string)
            if not math.isfinite(parsed_value):
                raise ValueError("A percentage must be finite.")
            if not 0 <= parsed_value <= 100:
                warning = emit_finding(
                    config,
                    tracker,
                    ErrorCode.EA_NUMBER_FORMAT_INVALID,
                    context={"atom_symbol": element, "context": f"{context} composition", "percentage_str": percentage_string},
                    variant="out_of_range_value",
                )
                if warning:
                    values.format_errors.append(warning)
                continue
            numeric_value = parsed_value
            values.numeric[element] = numeric_value
        except ValueError:
            warning = emit_finding(
                config,
                tracker,
                ErrorCode.EA_NUMBER_FORMAT_INVALID,
                context={"atom_symbol": element, "percentage_str": percentage_string},
                variant="non_numeric_value",
            )
            if warning:
                values.warnings.append(warning)
        if not is_two_decimal_percentage(percentage_string):
            location = f"calculated composition ({element})" if context == "calculated" else f"found composition({element})"
            warning = emit_finding(
                config,
                tracker,
                ErrorCode.EA_NUMBER_FORMAT_INVALID,
                context={"context": location, "percentage_str": percentage_string},
                variant="invalid_number_format",
            )
            if warning:
                values.format_errors.append(warning)
        elif numeric_value is not None:
            values.formatted[element] = numeric_value
    return values


def composition_differences(calculated: dict[str, float], found: dict[str, float]) -> list[tuple[str, float]]:
    """Return common-element differences in stable report order."""
    return [(element, round(abs(calculated[element] - found[element]), 2)) for element in sorted(calculated.keys() & found.keys())]


def exceeds_chn_tolerance(element: str, calculated: float, found: float) -> bool:
    """Apply the existing rounded 0.4 percentage-point CHN tolerance."""
    return element in {'C', 'H', 'N'} and round(abs(calculated - found), 2) > 0.4


def _compare_reported_compositions(
    config: AppConfig,
    tracker: ErrorTracker | FindingSink,
    calculated: dict[str, float],
    found: dict[str, float],
) -> tuple[list[tuple[str, float]], list[Diagnostic], list[Diagnostic]]:
    """Typed implementation behind `compare_reported_compositions`."""
    warnings = []
    tolerance_errors = []
    for elements, variant in [
        (calculated.keys() - found.keys(), "missing_in_found"),
        (found.keys() - calculated.keys(), "missing_in_calculated"),
    ]:
        for element in sorted(elements):
            warning = emit_finding(
                config,
                tracker,
                ErrorCode.EA_CALCD_FOUND_ELEMENT_MISMATCH,
                context={"element": element},
                variant=variant,
            )
            if warning:
                warnings.append(warning)

    matches = composition_differences(calculated, found)
    for element, _ in matches:
        if exceeds_chn_tolerance(element, calculated[element], found[element]):
            warning = emit_finding(
                config,
                tracker,
                ErrorCode.EA_TOLERANCE_EXCEEDED,
                context={"element": element, "diff": abs(calculated[element] - found[element]), "tolerance": 0.4},
            )
            if warning:
                tolerance_errors.append(warning)
    return matches, warnings, tolerance_errors


def _compare_formula_composition(
    config: AppConfig,
    tracker: ErrorTracker | FindingSink,
    theoretical: dict[str, float],
    calculated: dict[str, float],
    *,
    alternative: dict[str, float] | None = None,
) -> list[Diagnostic]:
    """Typed implementation behind `compare_formula_composition`."""
    warnings = []
    unmatched_isotope_vector = False
    absent_identity = set()
    reported_elements = set(calculated)
    percentage_values = calculated
    if alternative is not None:
        def element_symbol(label):
            symbol = label.lstrip('0123456789')
            return 'H' if symbol in ('D', 'T') else symbol
        formula_elements = {element_symbol(element) for element in theoretical}
        reported_elements = {element_symbol(element) for element in calculated}
        absent_identity = {element for element in calculated if element_symbol(element) not in formula_elements}
        for element in sorted(absent_identity):
            warning = emit_finding(config, tracker, ErrorCode.EA_FORMULA_CALCD_MISMATCH,
                context={'element': element}, variant='element_not_in_formula')
            if warning:
                warnings.append(warning)
        percentage_values = {element: value for element, value in calculated.items() if element not in absent_identity}
        def matches(values):
            return all(element in values and round(abs(percentage - values[element]), 2) <= 0.01
                       for element, percentage in percentage_values.items())
        if matches(theoretical):
            pass
        elif matches(alternative):
            theoretical = alternative
        else:
            def percentage_text(values):
                return ', '.join(f'{key} {value:.2f}%' for key, value in sorted(values.items()))
            warning = emit_finding(config, tracker, ErrorCode.EA_ISOTOPE_CALCD_MISMATCH,
                context={'calculated_percentages': percentage_text(percentage_values),
                         'mass_percentages': percentage_text(theoretical),
                         'atom_count_percentages': percentage_text(alternative)})
            if warning:
                warnings.append(warning)
            unmatched_isotope_vector = True
    for element, percentage in calculated.items():
        if unmatched_isotope_vector or element in absent_identity:
            continue
        if element in theoretical:
            delta = abs(percentage - theoretical[element])
            if not round(delta, 2) > 0.01:
                continue
            warning = emit_finding(
                config,
                tracker,
                ErrorCode.EA_FORMULA_CALCD_MISMATCH,
                context={
                    "element": element,
                    "formula_percent_value": theoretical[element],
                    "calculated_percent_value": percentage,
                    "delta": delta,
                },
                variant="formula_vs_calculated",
            )
        else:
            warning = emit_finding(
                config,
                tracker,
                ErrorCode.EA_FORMULA_CALCD_MISMATCH,
                context={"element": element},
                variant="element_not_in_formula",
            )
        if warning:
            warnings.append(warning)

    allowed_elements = {'C', 'H', 'N', 'S'}
    if get_error_severity(config, ErrorCode.EA_INCLUDE_OXYGEN) in {ErrorSeverity.WARNING, ErrorSeverity.CRITICAL}:
        allowed_elements.add('O')
    for element in sorted((theoretical.keys() & allowed_elements) - reported_elements):
        warning = emit_finding(
            config,
            tracker,
            ErrorCode.EA_FORMULA_ELEMENT_MISSING_IN_CALCD,
            context={"element": element},
        )
        if warning:
            warnings.append(warning)
    return warnings


def parse_composition_values(
    config: AppConfig,
    tracker: ErrorTracker,
    section: str | None,
    context: str,
) -> CompositionValues:
    """Parse EA values while retaining separate numeric and precision validity."""
    result = _parse_composition_values(config, tracker, section, context)
    return CompositionValues(result.numeric, result.formatted, messages(result.warnings), messages(result.format_errors))


def compare_reported_compositions(
    config: AppConfig,
    tracker: ErrorTracker,
    calculated: dict[str, float],
    found: dict[str, float],
) -> tuple[list[tuple[str, float]], list[str], list[str]]:
    """Compare valid calculated/found fields and emit existing findings."""
    matches, warnings, tolerance = _compare_reported_compositions(config, tracker, calculated, found)
    return matches, messages(warnings), messages(tolerance)


def compare_formula_composition(
    config: AppConfig,
    tracker: ErrorTracker,
    theoretical: dict[str, float],
    calculated: dict[str, float],
) -> list[str]:
    """Compare numeric calculated values to theory, preserving oxygen policy."""
    return messages(_compare_formula_composition(config, tracker, theoretical, calculated))
