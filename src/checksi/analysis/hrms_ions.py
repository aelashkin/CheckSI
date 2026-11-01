"""Internal HRMS ion-description parsing and inverse composition accounting."""

from itertools import islice
import math
import re

from molmass import Formula, FormulaError

from checksi.analysis.formula_utils import replace_common_isotope_formats
from checksi.analysis.formula_isotopes import (
    _NATURAL_ISOTOPE_ELEMENTS, _comparison_counts,
)
from checksi.analysis.hrms_fields import (
    HRMS_INSTRUMENT_PATTERN, _charge_suffix, balanced_group_end,
    find_hrms_formula_candidate, iter_ion_expression_spans, parse_charge,
)
from checksi.analysis.formula_source_policy import _ANIONS, _FORCED_DISTINCT, _SOLVENTS
from checksi.config_schema import AppConfig, OutputMode
from checksi.errors import ErrorCode
from checksi.errors.manager import emit_finding
from checksi.errors.findings import AnalysisNote, messages, FindingSink
from checksi.utils.error_handler import ErrorTracker


def extract_ion_fragments(text: str) -> list[tuple[str, str]] | None:
    """Read complete signed adduct fragments, retaining bracketed isotope labels."""
    match = re.search(r'[\[(]M(?=[\s+\-–—•)\]])', text)
    if not match:
        return None
    end = balanced_group_end(text, match.start())
    if end is None:
        return None
    modifications = text[match.end():end - 1].strip()
    if not modifications or re.fullmatch(r'(?:[•·]\s*[+\-–—]?|[+\-–—]\s*[•·])', modifications):
        return []
    # Accepted in-bracket charge and EI radical-cation forms carry no atoms.
    modifications = re.sub(r'•?[+\-–—]$', "", modifications).strip()
    if not modifications:
        return []
    fragments = []
    end = 0
    for fragment in re.finditer(r'([+\-–—])\s*([^+\-–—]+)', modifications):
        if modifications[end:fragment.start()].strip():
            return None
        formula = re.sub(r'\s+', "", fragment.group(2))
        # Preserve the established inline halogen-isotope notation at the
        # beginning of a fragment; 2H remains a coefficient unless bracketed.
        formula = _leading_isotope(formula)
        fragments.append((fragment.group(1), replace_common_isotope_formats(formula)))
        end = fragment.end()
    if not fragments or modifications[end:].strip():
        return None
    return fragments


def _element_counts(formula: Formula) -> dict[str, int]:
    """Keep isotope-specific composition keys rather than dataframe display values."""
    return {element: entry.count for element, entry in formula.composition().items()}


def _leading_isotope(fragment: str) -> str:
    """Disambiguate documented inline labels; 2H remains a coefficient."""
    return re.sub(r'^(13C|15N|18O|35Cl|37Cl|79Br|81Br)(?![a-z])', r'[\1]', fragment)


def _fragment_counts(fragment: str) -> dict[str, int]:
    """Read an adduct's leading number as a coefficient, with bracketed isotopes fixed."""
    fragment = _leading_isotope(fragment)
    multiplier = re.match(r'\d+(?=[A-Za-z[(])', fragment)
    if not multiplier:
        fragment = _ANIONS.get(fragment, fragment)
    if multiplier:
        atom_text = fragment[multiplier.end():]
        fragment = f'({_ANIONS.get(atom_text, atom_text)}){multiplier.group()}'
    return _element_counts(Formula(fragment))


def _formula_from_counts(counts: dict[str, int]) -> str:
    """Serialize an already valid composition without losing isotope brackets."""
    parts = []
    for element, count in counts.items():
        if count == 0:
            continue
        symbol = f"[{element}]" if element[0].isdigit() else element
        parts.append(symbol if count == 1 else f"{symbol}{count}")
    return "".join(parts)


_M_ION = re.compile(r'[\[(]M(?=[\s+\-–—•)\]])')
_ION_SUFFIX = re.compile(r'\s*[•·]?')
_CHARGE_BASIS_REASON = 'A consistent supported charge on the ion is unavailable.'
# Bound per-ion pairing work; longer ion listings keep the explicit refusal.
_MAX_OWNED_IONS = 8


def _ion_charges(text: str, match: re.Match) -> tuple[set[int] | None, str | None]:
    """Return the ion's charge signs, or the reason its charge state is unsupported."""
    end = balanced_group_end(text, match.start())
    if end is None:
        return None, 'The ion bracket state is unresolved.'
    # A signed number after M or an adduct is a nominal mass offset, not a
    # charge magnitude. The bracket suffix owns signed magnitudes.
    internal = re.search(r'([+\-–—])$', text[match.end():end - 1].strip().rstrip('•·'))
    suffix = _ION_SUFFIX.match(text, end).end()
    external, _, _, supported = _charge_suffix(text, suffix)
    if external is not None and not supported:
        return None, 'The complete ion charge assertion is outside the supported signed-magnitude grammar.'
    charges = {parse_charge(raw) for raw in (internal.group(1) if internal else None, external) if raw}
    if None in charges:
        return None, 'The complete ion charge assertion is outside the supported signed-magnitude grammar.'
    return charges, None


def _inferred_adduct_charge(fragments):
    """Sum charges of the adjudicated ion units, keeping neutral losses neutral."""
    if fragments is None:
        return None
    positive = ('H', 'Li', 'Na', 'K', 'Cs', 'Ag', 'NH4')
    negative = (*_ANIONS.values(), 'CHO2', 'C2H3O2')
    neutral = (*_SOLVENTS.values(),)
    def ordinary(counts):
        result = {}
        for key, count in counts.items():
            symbol = re.sub(r'^\d+', '', key)
            result[symbol] = result.get(symbol, 0) + count
        return result
    units = [(ordinary(_fragment_counts(formula)), charge)
             for formulas, charge in ((positive, 1), (negative, -1), (neutral, 0)) for formula in formulas]
    total = 0
    for sign, fragment in fragments:
        fragment = _leading_isotope(fragment)
        match = re.match(r'(\d+)(?=[A-Za-z[(])', fragment)
        if match and len(match.group(1)) > 6:
            return None
        coefficient = int(match.group(1)) if match else 1
        body = fragment[match.end():] if match else fragment
        try:
            counts = ordinary(_fragment_counts(body))
        except (FormulaError, ValueError, OverflowError):
            return None
        unit = next((charge for atoms, charge in units if atoms == counts), None)
        if unit is None:
            return None
        total += (1 if sign == '+' else -1) * coefficient * unit
    return total


def charge_evidence(text: str, candidate=None) -> dict:
    """Select the declared formula charge first, then written/inferred ion charge."""
    formula_charge = parse_charge(candidate.charge_raw) if candidate is not None else None
    scope = text[candidate.header_start:] if candidate is not None else text
    boundary = re.search(r'\bNMR\b|\bAnal\.?\s*calcd\.?', scope, re.IGNORECASE)
    scope = scope[:boundary.start()] if boundary else scope
    spans = iter_ion_expression_spans(scope)
    first = next(spans, None)
    charges, reason = set(), None
    if first is not None:
        start, _ = first
        match = re.match(r'[\[(](?:\d+)?M(?=[\s+\-–—•)\]])', scope[start:])
        if match:
            # Rebase the small match so its offsets remain in the owning scope.
            match = re.compile(r'[\[(](?:\d+)?M(?=[\s+\-–—•)\]])').match(scope, start)
            charges, reason = _ion_charges(scope, match)
        else:
            end = balanced_group_end(scope, start)
            raw, _, _, supported = _charge_suffix(scope, end) if end is not None else (None, None, None, None)
            charges = {parse_charge(raw)} if supported else set()
            reason = 'Unsupported ion charge assertion.' if raw and not supported else None
    fragments = extract_ion_fragments(scope)
    inferred = _inferred_adduct_charge(fragments)
    ion_charge = next(iter(charges)) if charges and len(charges) == 1 else None
    supplied_ion = ion_charge if ion_charge is not None else (inferred or None)
    effective = formula_charge if formula_charge is not None else supplied_ion
    unsupported = bool(reason or (candidate is not None and candidate.charge_raw and not candidate.charge_supported))
    return {'formula': formula_charge, 'ion': supplied_ion, 'effective': effective,
            'ion_assertions': tuple(sorted(charges)) if charges else (),
            'inferred': inferred, 'unsupported': unsupported,
            'formula_charge_missing': formula_charge is None and not (
                candidate is not None and candidate.charge_raw),
            'ion_charge_missing': first is not None and not charges and not reason,
            'conflict': bool((charges and len(charges) > 1) or (formula_charge is not None
                and supplied_ion is not None and formula_charge != supplied_ion)),
            # This combined flag owns unresolved-state review suppression only;
            # written formula/adduct notation is checked independently above.
            'missing': formula_charge is None and not charges and not unsupported}


def _single_charge_state(charges: set[int] | None, candidate,
                         fragments: list[tuple[str, str]]) -> bool:
    """Use a conventional single-charge adduct when charge text is incomplete."""
    formula_charge = parse_charge(candidate.charge_raw) if candidate is not None else None
    if charges is None or len(charges) > 1:
        return False
    if formula_charge is not None and charges and charges != {formula_charge}:
        return False
    if formula_charge is not None or charges:
        return True
    return bool(_inferred_adduct_charge(fragments))


def _apply_inverse_count(counts: dict[str, int], sign: str, element: str, count: int) -> str | None:
    """Apply one adduct count, retaining protium labels and excluding enrichment."""
    if sign != '+':
        counts[element] = counts.get(element, 0) + count
        return None
    if element in _NATURAL_ISOTOPE_ELEMENTS:
        ordinary = _NATURAL_ISOTOPE_ELEMENTS[element]
        # An unspecified atom has the library's monoisotopic mass. Only an
        # identical explicit isotope may consume it without changing that mass.
        keys = (element, ordinary) if abs(Formula(f'[{element}]').isotope.mass - Formula(ordinary).isotope.mass) < 1e-8 else (element,)
    else:
        keys = (element, *(key for key, value in _NATURAL_ISOTOPE_ELEMENTS.items() if value == element))
    eligible = sum(counts.get(key, 0) for key in keys)
    if eligible < count:
        symbol = re.sub(r'^\d+', '', element)
        physical = {key: value for key, value in counts.items() if re.sub(r'^\d+', '', key) == symbol}
        if not physical:
            return 'element_not_found'
        if sum(physical.values()) >= count and element not in _FORCED_DISTINCT:
            uncovered = sum(value for key, value in physical.items() if key[0].isdigit()
                            and key not in _NATURAL_ISOTOPE_ELEMENTS and key not in _FORCED_DISTINCT)
            required_uncovered = element[0].isdigit() and element not in _NATURAL_ISOTOPE_ELEMENTS
            if required_uncovered or eligible + uncovered >= count:
                return 'isotope_unadjudicated'
        return 'negative_quantity'
    alternatives = [key for key in keys[1:] if counts.get(key, 0)]
    remaining_after_exact = max(0, count - counts.get(element, 0))
    if (remaining_after_exact and len(alternatives) > 1
            and remaining_after_exact != sum(counts[key] for key in alternatives)
            and len({Formula(f'[{key}]').isotope.mass if key[0].isdigit()
                     else Formula(key).isotope.mass for key in alternatives}) > 1):
        return 'isotope_ambiguous'
    remaining = count
    for key in keys:
        removed = min(counts.get(key, 0), remaining)
        if removed:
            counts[key] -= removed
            remaining -= removed
    return None


def _inverse_counts(formula: str, fragments: list[tuple[str, str]]) -> dict[str, int] | None:
    """Apply the existing inverse adduct accounting without emitting findings."""
    try:
        counts = _element_counts(Formula(formula))
        for sign, fragment in fragments:
            for element, count in _fragment_counts(fragment).items():
                if _apply_inverse_count(counts, sign, element, count) is not None:
                    return None
    except (FormulaError, OverflowError, ValueError):
        return None
    return {element: count for element, count in counts.items() if count}


def _grouped(fragments: list[tuple[str, str]]) -> bool:
    return any(re.search(r'[.·()\[\]]', re.sub(r'\[\d+[A-Z][a-z]?\]', '', fragment)) for _, fragment in fragments)


def _owned_ions_share_neutral(text: str, candidate, ions: list[tuple[int, int]], owned_end: int,
                              neutral: dict[str, int]) -> bool:
    """Require every owned ion to invert its own formula to the same neutral."""
    if len(ions) > _MAX_OWNED_IONS:
        return False
    formulas = [candidate]
    while len(formulas) < len(ions):
        previous = formulas[-1]
        # Blank earlier fields, keeping offsets and the owning instrument marker.
        blanked = text[:previous.header_end] + ' ' * (previous.end - previous.header_end) + text[previous.end:]
        following = find_hrms_formula_candidate(blanked)
        if (following is None or following.header_start != candidate.header_start
                or following.start < previous.end or following.start >= owned_end):
            break
        formulas.append(following)
    if len(formulas) == 1:
        formulas *= len(ions)
    if len(formulas) != len(ions):
        return False
    for (start, end), formula in zip(ions, formulas):
        match = _M_ION.match(text, start)
        fragments = extract_ion_fragments(text[start:end]) if match else None
        if fragments is None or formula.formula is None or formula.charge_supported is False or _grouped(fragments):
            return False
        charges, _ = _ion_charges(text, match)
        if not _single_charge_state(charges, formula, fragments):
            return False
        inferred = _inferred_adduct_charge(fragments)
        effective = (parse_charge(formula.charge_raw)
                     or (next(iter(charges)) if charges else inferred))
        if inferred is None or effective != inferred:
            return False
        recovered = _inverse_counts(replace_common_isotope_formats(formula.formula), fragments)
        if (recovered is None
                or _comparison_counts(recovered.items()) != _comparison_counts(neutral.items())):
            return False
    return True


def _neutral_state(text: str, fragments: list[tuple[str, str]], neutral: dict[str, int]) -> tuple[bool, str]:
    """Require a supported single ion charge without inferring component state."""
    match = _M_ION.search(text)
    if match is None:
        return False, 'The ion expression is unavailable.'
    ion_charges, reason = _ion_charges(text, match)
    if reason is not None:
        return False, reason
    candidate = find_hrms_formula_candidate(text)
    if candidate is not None:
        if candidate.charge_supported is False:
            return False, 'The complete formula-side charge assertion is outside the supported single-charge comparison basis.'
        if match.start() < candidate.header_start:
            return False, 'The inverse ion expression precedes the formula\'s owning instrument marker.'
        intervening = text[min(match.start(), candidate.start):max(match.start(), candidate.start)]
        if re.search(r'\bNMR\b|\bAnal\.?\s*calcd\.?', intervening, re.IGNORECASE):
            return False, 'The inverse ion expression is not in the formula\'s analytical section.'
        section_end = re.search(r'\bNMR\b|\bAnal\.?\s*calcd\.?', text[candidate.end:], re.IGNORECASE)
        owned_end = candidate.end + section_end.start() if section_end else len(text)
        ions = list(islice(iter_ion_expression_spans(text, candidate.header_start, owned_end), _MAX_OWNED_IONS + 1))
        if not ions or (len(ions) > 1 and not _owned_ions_share_neutral(text, candidate, ions, owned_end, neutral)):
            return False, 'Multiple owned ion/adduct assertions do not establish an unambiguous neutral comparison basis.'
    if not _single_charge_state(ion_charges, candidate, fragments):
        return False, _CHARGE_BASIS_REASON
    if _grouped(fragments):
        return False, 'Joined or grouped adduct state is unresolved for source comparison.'
    return True, 'Recovered by inverse adduct accounting with a supported single charge or conventional adduct.'


def molecular_radical_evidence(text: str) -> bool:
    """Qualify EI or explicit radical notation in this formula's owned section."""
    candidate = find_hrms_formula_candidate(text)
    if candidate is None:
        scope = text
        instrument = text
        prefix = ''
    else:
        scope = text[candidate.header_start:]
        boundaries = re.finditer(rf'\bNMR\b|\bAnal\.?\s*calcd\.?|\b(?:{HRMS_INSTRUMENT_PATTERN})\b',
                                 text[candidate.header_end:], re.IGNORECASE)
        for boundary in boundaries:
            start = candidate.header_end + boundary.start()
            # A post-formula '(ESI, [M]•+)' is an owned ion qualifier. A new
            # standalone instrument section cannot donate radical evidence.
            qualifier = re.fullmatch(r'(?:APCI|ESI|MALDI)(?:[\s-]*(?:TOF|MS))?', boundary.group(), re.IGNORECASE)
            if (qualifier and start > candidate.end
                    and re.fullmatch(r'\s*[\[(]\s*', text[candidate.charge_end or candidate.end:start])):
                continue
            scope = text[candidate.header_start:start]
            break
        prefix = text[max(0, candidate.header_start - 16):candidate.header_start]
        instrument = text[candidate.header_start:candidate.start]
    return bool(re.search(r'\bEI\b|HR[\s-]*EI[\s-]*MS|EI[\s-]*(?:HRMS|TOF)', instrument, re.IGNORECASE)
                or re.search(r'\bEI[\s-]*$', prefix, re.IGNORECASE)
                or re.search(r'[\[(]\s*M\s*(?:[•·]\s*[+\-]?|[+\-]\s*[•·])\s*[\])]|'
                             r'[\[(]\s*M\s*[\])]\s*(?:[+\-]\s*[•·]|[•·]\s*[+\-])', scope))


def _adjust_ion_composition(cfg: AppConfig, tracker: ErrorTracker | FindingSink, text, output, formula, real_mass, *, details: dict | None = None, original_text: str | None = None):
    """Typed implementation behind `adjust_ion_composition`."""
    candidate = find_hrms_formula_candidate(text)
    ion_text = text[candidate.header_start:] if candidate is not None else text
    if extract_ion_fragments(ion_text) is None:
        ion_text = text
    fragments = extract_ion_fragments(ion_text)
    if details is not None:
        details.clear()
        details.update(status='unavailable', formula=None, mass=None, usable=False,
                       fragments=[list(fragment) for fragment in fragments] if fragments is not None else None,
                       comparable=False, state='unknown',
                       reason='The inverse adduct composition is unavailable or invalid.',
                       derivation='existing_inverse_adduct')
    if fragments is None:
        return output, None, None
    try:
        formula_hrms = Formula(formula)
        formula_element_counts = _element_counts(formula_hrms)
    except (FormulaError, OverflowError, ValueError):
        return output, None, None

    parsed_fragments = []
    all_fragments_valid = True
    for sign, fragment in fragments:
        try:
            parsed_fragments.append((sign, _fragment_counts(fragment)))
        except (FormulaError, OverflowError, ValueError):
            all_fragments_valid = False

    # Identity diagnostics remain available even if another fragment is invalid.
    positive_elements = {
        element for sign, fragment in fragments if sign == "+"
        for element in re.findall(r'[A-Z][a-z]?', fragment)
    }
    # Compare element identities, so krypton is not mistaken for potassium.
    for element, plus_variant, formula_variant in [
        ("Na", "m_plus_na_but_no_na", "formula_has_na_but_not_ion"),
        ("K", "m_plus_k_but_no_k", "formula_has_k_but_not_ion"),
    ]:
        has_adduct = element in positive_elements
        has_element = any(re.sub(r'^\d+', '', key) == element for key in formula_element_counts)
        if has_adduct != has_element:
            warning = emit_finding(
                cfg, tracker, ErrorCode.HRMS_ADDUCT_MISMATCH,
                variant=plus_variant if has_adduct else formula_variant,
            )
            if warning:
                output.append(warning)

    for sign, ion_element_counts in parsed_fragments:
        for element, count in ion_element_counts.items():
            failure = _apply_inverse_count(formula_element_counts, sign, element, count)
            if failure is not None:
                if failure in ('isotope_ambiguous', 'isotope_unadjudicated'):
                    if details is not None:
                        details.update(reason=('The adduct does not specify which natural isotope was removed.'
                            if failure == 'isotope_ambiguous' else 'The adduct isotope relationship is outside the adjudicated natural-isotope policy.'))
                    return output, None, None
                warning = emit_finding(
                    cfg, tracker, ErrorCode.HRMS_ION_BALANCE_INVALID,
                    context={"elem": element}, variant=failure,
                )
                if warning:
                    output.append(warning)
                if details is not None:
                    details.update(status='invalid', reason='The inverse adduct has an invalid atom balance.')
                return output, None, None

    if not all_fragments_valid:
        return output, None, None
    try:
        real_formula = _formula_from_counts(formula_element_counts)
        if not real_formula:
            return output, None, None
        real_mass = Formula(real_formula).isotope.mass
    except (FormulaError, OverflowError, ValueError):
        return output, None, None
    if not math.isfinite(real_mass):
        return output, None, None
    if details is not None:
        comparable, reason = _neutral_state(
            ion_text, fragments, {element: count for element, count in formula_element_counts.items() if count})
        evidence = charge_evidence(ion_text, find_hrms_formula_candidate(ion_text))
        parent_charge = (evidence['effective'] - evidence['inferred']
                         if evidence['effective'] is not None and evidence['inferred'] is not None else None)
        molecular_ion = comparable and not fragments
        is_radical = molecular_radical_evidence(original_text or text)
        if molecular_ion and is_radical:
            parent_charge = 0
        if molecular_ion and parent_charge:
            reason = 'The molecular-ion formula is retained as a cation or anion; no neutral atom correction was inferred.'
        elif comparable and parent_charge is None:
            reason = 'The parent charge is not established by this adduct; its atom inventory is retained for review.'
        details.update(status='recovered', formula=real_formula, mass=real_mass, usable=True,
                       comparable=comparable and parent_charge == 0,
                       state='ion' if comparable and parent_charge else 'neutral' if comparable and parent_charge == 0 else 'unknown',
                       charge=parent_charge,
                       derivation='molecular_ion' if molecular_ion else 'existing_inverse_adduct', reason=reason)
        if evidence['conflict'] and comparable:
            # The precise charge diagnostic already owns this uncertainty.
            # Keep the atom inventory, without a second generic review finding.
            details.update(comparable=False, state='unknown', reason=_CHARGE_BASIS_REASON,
                           review_required=False)
        elif reason == _CHARGE_BASIS_REASON and (evidence['missing'] or evidence['conflict']):
            details['review_required'] = False
    if cfg.modes.output_mode == OutputMode.DETAILED:
        output.append(AnalysisNote(f"formula HRMS {formula_hrms}, real formula {real_formula}"))
    return output, real_formula, real_mass


def adjust_ion_composition(cfg: AppConfig, tracker: ErrorTracker, text, output, formula, real_mass, *, details: dict | None = None):
    """Recover a neutral composition; stop at the first invalid inverse element."""
    diagnostics = []
    _, first, second = _adjust_ion_composition(cfg, tracker, text, diagnostics, formula, real_mass, details=details)
    output.extend(messages(diagnostics))
    return output, first, second
