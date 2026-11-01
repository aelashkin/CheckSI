"""Shared complete formula candidates and ownership of analytical fields."""

import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass

from periodictable import elements as _periodic_elements
from checksi.analysis.formula_source_policy import parse_ea_formula


HRMS_INSTRUMENT_PATTERN = (r'HR[\s-]*EI[\s-]*MS|EI[\s-]*HRMS|EI[\s-]*TOF|Q[\s-]*TOF|HRMS|'
                           r'(?:APCI|ESI|MALDI)(?:[\s-]*(?:TOF|MS))?|TOF')
_INSTRUMENT = re.compile(HRMS_INSTRUMENT_PATTERN, re.IGNORECASE)
_OTHER_ANALYSIS = re.compile(r'\bNMR\b|\bAnal\.?\s*calcd\.?|\[\s*α\s*\]\s*D', re.IGNORECASE)
_FORMULA = re.compile(r'C\d++(?:[A-Za-z0-9]|\[\d++[A-Z][a-z]?\]\d*+)*+', re.IGNORECASE)
_EA_FORMULA = re.compile(r'C\d++(?:[A-Za-z0-9]|\[\d++[A-Z][a-z]?\]\d*+)*+', re.IGNORECASE)
_EA_HEADER = re.compile(r'Anal\.?\s*calcd\.?\s*for\s*', re.IGNORECASE)
_ISOTOPE = re.compile(r'\[\d+[A-Z][a-z]?\]')
# A bracket seeds a token only when it holds an uppercase C (a formula or isotope
# group) or directly precedes a formula. Citations ([12], [3, 4–6], [S3], [ref. 12])
# and labels such as [α]D are not formula tokens.
_SEED = re.compile(
    r'C(?=\d|\[)|(?<![A-Za-z])(?-i:C(?=[A-Z]))|\[(?=[^\[\]]*(?-i:C))[^\[\]]*\]'
    r'|\[(?![\d\s,–—-]+\])[^\[\]]*\](?=\s*(?-i:C)(?:\d|\[))',
    re.IGNORECASE,
)
_CALCULATED = re.compile(r"\b(?:calcd\.?|calculated|calc['’]d|calc\.?)(?![A-Za-z])\s*(?:mass\s+)?(?:\d+(?:\.\d+)?\s+)?(?:for\s+)?", re.IGNORECASE)
_FIELD = re.compile(rf'(?:\b(?:found|calcd|calculated|NMR|Anal|{HRMS_INSTRUMENT_PATTERN})\b|\d+[A-Z][a-z]?\s+NMR\b|m/z)', re.IGNORECASE)
_MASS = re.compile(r'[+\-–—]?(?>\d+(?:\.\d*)?)(?!\w)')
_AND = re.compile(r'and\s++')
_CHARGE_DIGITS = re.compile(r'\d+[+\-–—]+')
_ION_START = re.compile(
    rf'(?:\(\s*(?:(?:{HRMS_INSTRUMENT_PATTERN})\s*,?\s*)?)?'
    r'(?P<ion>[\[(]\s*(?:\d+)?M(?=[\s+\-–—•)\]]|(?:H|Na|K)(?=[+\-–—)\]]))|'
    r'\[(?=(?:[A-Za-z0-9]|\[\d++[A-Z][a-z]?\])*?(?-i:C(?![a-z])|\[\d++C\]))'
    r'(?:[A-Za-z0-9]|\[\d++[A-Z][a-z]?\])*+\]\s*(?:\d++[+\-–—]|[+\-–—]))',
    re.IGNORECASE,
)
_INSTRUMENT_LABEL = re.compile(rf'\[\s*(?:{HRMS_INSTRUMENT_PATTERN})(?:[-/\s]+(?:{HRMS_INSTRUMENT_PATTERN}))*\s*\]', re.IGNORECASE)
# Lower-case words that declare a salt, solvate, multiplier or joined component
# continue the complete token; other lower-case words (connectors, prose) end it.
_STATE_WORD = re.compile(
    r'(?:[a-z]*(?:ates?|ides?|ites?|ium)|salts?|complex(?:es)?|adducts?|x|n|with|and|plus|containing'
    r'|water|[a-z]*(?:methanol|ethanol|propanol|butanol)|acetone|acetonitrile|ether|chloroform'
    r'|dichloromethane|toluene|benzene|hexanes?|pentane|heptane|dioxane|pyridine)(?![A-Za-z])'
)
# Metadata stops at the next bracket, keeping repeated unclosed '(MW' runs linear.
_EA_FIELD = re.compile(r'\((?:M\.?W\.?\s*[^()]*+|cf\.\s*ref\.[^()]*+|\s*\d+(?:\.\d+)?\s*|\s*%\s*)\)|[A-Z][a-z]?(?:\s*,|\s+[+\-]?\d)', re.IGNORECASE)


NOTATION_NOT_SUPPORTED = 'notation_not_supported'
_ELEMENT_SYMBOLS = frozenset(element.symbol for element in _periodic_elements if element.number) | {'D', 'T'}
# Common group and placeholder abbreviations are notation, not unknown elements.
_ABBREVIATIONS = frozenset({
    'Me', 'Et', 'Bu', 'Ph', 'Bn', 'Bz', 'Cy', 'Cp', 'Py', 'Tf', 'Ms', 'Hx',
    'Ad', 'Fc', 'Tr', 'Ns', 'Bs', 'Nf', 'L', 'R', 'X', 'M', 'Ln',
})
_PLAIN_FORMULA = re.compile(r'(?:[A-Z][a-z]?(?:\d+(?:\.\d+)?)?)+')
_PLAIN_TERM = re.compile(r'([A-Z][a-z]?)(\d+(?:\.\d+)?)?')
_ATOM = re.compile(r'(\[\d++[A-Z][a-z]?\]|[A-Z][a-z]?)(\d*+)')
_REFUSAL_REASONS = {
    'unknown_element': "Unknown element symbol '{}'.",
    'zero_count': "Zero atom count '{}'.",
    'non_integer_count': "Atom count '{}' is not a whole number.",
    'unbalanced_brackets': "Unbalanced bracket '{}' inside the formula.",
    NOTATION_NOT_SUPPORTED: ('Not checked by CheckSI: the complete token uses formula notation outside '
                             'the supported grammar; this is not a finding about the formula.'),
}


def classify_refusal(raw: str, source: str = 'hrms') -> tuple[str, str | None]:
    """Name a definite formula-rule violation; anything else was not checked."""
    if _PLAIN_FORMULA.fullmatch(raw):
        terms = _PLAIN_TERM.findall(raw)
        symbols = [symbol for symbol, _ in terms]
        # A repeated symbol may be a joined component (for example a '.' separator).
        if len(set(symbols)) == len(symbols):
            unknown = [symbol for symbol in symbols if symbol not in _ELEMENT_SYMBOLS | _ABBREVIATIONS]
            if unknown:
                return 'unknown_element', unknown[0]
            if any(symbol not in _ELEMENT_SYMBOLS for symbol in symbols):
                return NOTATION_NOT_SUPPORTED, None
            for kind, test in (('zero_count', lambda count: float(count) == 0),
                               ('non_integer_count', lambda count: '.' in count)):
                for symbol, count in terms:
                    if count and test(count):
                        if kind == 'non_integer_count' and source == 'ea':
                            policy = parse_ea_formula(raw)
                            if policy.status != 'typo':
                                return NOTATION_NOT_SUPPORTED, None
                        return kind, symbol + count
        return NOTATION_NOT_SUPPORTED, None
    if re.fullmatch(r'[A-Za-z0-9()\[\]]+', raw):
        stack = []
        for character_index, character in enumerate(raw):
            if character in '([':
                stack.append(character_index)
            elif character in ')]' and stack and raw[stack[-1]] == {')': '(', ']': '['}[character]:
                stack.pop()
        # Only a bracket opened inside the formula and never closed is definite;
        # a stray closer or an edge bracket may belong to the surrounding text.
        internal = [index for index in stack if 0 < index < len(raw) - 1]
        if internal:
            return 'unbalanced_brackets', raw[internal[0]]
    return NOTATION_NOT_SUPPORTED, None


@dataclass(frozen=True)
class HRMSFormulaMatch:
    """Original supported token and the instrument marker owning its fields."""

    formula: str
    header_start: int
    header_end: int
    formula_start: int
    formula_end: int


@dataclass(frozen=True)
class FormulaCandidate:
    """Complete source assertion before applying the existing source grammar."""

    raw: str
    start: int
    end: int
    source: str
    header_start: int
    header_end: int
    formula: str | None
    reason: str | None
    charge: str | None = None
    charge_raw: str | None = None
    charge_start: int | None = None
    charge_end: int | None = None
    charge_supported: bool | None = None
    refusal: str | None = None
    refusal_detail: str | None = None

    def to_dict(self) -> dict:
        """Return JSON-safe input-relative evidence without diagnostic policy."""
        return asdict(self)


def _simple_carbon_formula(raw: str) -> bool:
    """Recognize a complete contiguous atom inventory, including leading labels."""
    cursor = 0
    seen = set()
    carbon = False
    for match in _ATOM.finditer(raw):
        if match.start() != cursor:
            return False
        atom, count = match.groups()
        symbol = re.sub(r'^\[\d+|\]$', '', atom) if atom.startswith('[') else atom
        key = '[2H]' if atom == 'D' else '[3H]' if atom == 'T' else atom
        if symbol not in _ELEMENT_SYMBOLS or key in seen or (count and not count.strip('0')):
            return False
        seen.add(key)
        carbon |= symbol == 'C'
        cursor = match.end()
    return bool(carbon and cursor == len(raw))


def _literal_formula_end(text: str, start: int) -> int | None:
    """Recognize an outer formula-ion bracket without stripping notation groups."""
    if start >= len(text) or text[start] != '[':
        return None
    end = balanced_group_end(text, start)
    if end is None or not _simple_carbon_formula(text[start + 1:end - 1]):
        return None
    return end if _charge_suffix(text, end)[0] is not None else None


def _mask_ion_brackets(text: str) -> str:
    """Hide balanced ion brackets, retaining isotope atoms and original offsets."""
    stack = []
    closing = {}
    for index, character in enumerate(text):
        if character == '[':
            stack.append(index)
        elif character == ']' and stack:
            closing[stack.pop()] = index + 1
    chunks = []
    index = 0
    while index < len(text):
        end = closing.get(index, index + 1)
        token = text[index:end]
        literal_end = _literal_formula_end(text, index) if index in closing else None
        following = (_skip_space(text, _charge_suffix(text, literal_end)[2])
                     if literal_end is not None else len(text))
        separate_formula = _SEED.match(text, following) is not None
        if (index in closing and not _ISOTOPE.fullmatch(token) and _ION_START.match(text, index)
                and (literal_end is None or separate_formula)):
            chunks.append(' ' * len(token))
        else:
            # An unmatched '[' is ordinary text, not a reason to hide the suffix.
            chunks.append(token)
        index = end
    return ''.join(chunks)


def _skip_space(text: str, start: int) -> int:
    while start < len(text) and text[start].isspace():
        start += 1
    return start


def _mass_boundary(text: str, start: int) -> bool:
    """Keep numeric measurement precision separate from formula continuation."""
    mass = _MASS.match(text, start)
    if mass is None:
        return False
    following = _skip_space(text, mass.end())
    joined = _AND.match(text, following)
    return (following == len(text) or text[following] in '.,;:)]'
            or _FIELD.match(text, following) is not None or _ION_START.match(text, following) is not None
            or (joined is not None and _FIELD.match(text, joined.end()) is not None))


def balanced_group_end(text: str, start: int, limit: int | None = None) -> int | None:
    """Return the complete bracket-group boundary, refusing mismatched groups."""
    limit = len(text) if limit is None else limit
    if start >= limit or text[start] not in '[(':
        return None
    stack = []
    for cursor in range(start, limit):
        character = text[cursor]
        if character in '[(':
            stack.append(']' if character == '[' else ')')
        elif character in '])':
            if not stack or character != stack.pop():
                return None
            if not stack:
                return cursor + 1
    return None


def _skip_ion_labels(text: str, start: int, limit: int) -> int:
    """Keep established ion labels between the field cue and its formula."""
    while start < limit:
        start = _skip_space(text, start)
        if start >= limit or text[start] not in '[(':
            break
        if not _ION_START.match(text, start) and not (text.startswith('([', start) and _ION_START.match(text, start + 1)):
            break
        end = balanced_group_end(text, start, limit)
        if end is None:
            return start
        _, _, charge_end, _ = _charge_suffix(text, end)
        if charge_end is not None:
            end = charge_end
        if end < limit and text[end] in '•·':
            end += 1
        start = end
    return start


def iter_ion_expression_spans(text: str, start: int = 0, end: int | None = None) -> Iterator[tuple[int, int]]:
    """Yield distinct ion cores, preserving nested isotopes and wrapper siblings."""
    limit = len(text) if end is None else end
    consumed = start
    for match in _ION_START.finditer(text, start, limit):
        ion_start = match.start('ion')
        if ion_start < consumed:
            continue
        cursor = balanced_group_end(text, ion_start, limit)
        if cursor is None:
            cursor = limit
        elif cursor < limit and text[cursor] == '•':
            cursor += 1
        _, _, charge_end, _ = _charge_suffix(text, cursor)
        if charge_end is not None:
            cursor = charge_end
        consumed = max(cursor, match.end())
        yield ion_start, consumed


def _field_follows(text: str, start: int, source: str) -> bool:
    """Recognize a measurement, analytical field or connector word after whitespace."""
    return (_mass_boundary(text, start) or _FIELD.match(text, start) is not None
            or (source == 'ea' and _EA_FIELD.match(text, start) is not None)
            or (start < len(text) and text[start].islower() and not _STATE_WORD.match(text, start)))


def _period_boundary(text: str, start: int, source: str) -> bool:
    following = _skip_space(text, start + 1)
    # A '.' between digits is a decimal count inside the token, unless an NMR label follows.
    if (0 < start and text[start - 1].isdigit() and following == start + 1 < len(text)
            and text[following].isdigit() and _FIELD.match(text, following) is None):
        return False
    return (following == len(text) or text[following] in ':;,'
            or _FIELD.match(text, following) is not None or _mass_boundary(text, following)
            or (source == 'ea' and _EA_FIELD.match(text, following) is not None))


def _instrument_closing_end(text: str, header) -> int:
    """Recognize closing punctuation around an instrument label only."""
    end = _skip_space(text, header.end())
    if end < len(text) and text[end] in ')]':
        opening = '(' if text[end] == ')' else '['
        start = text.rfind(opening, 0, header.start() + 1)
        if start >= 0 and _INSTRUMENT_LABEL.fullmatch('[' + text[start + 1:end] + ']'):
            return end + 1
    return header.end()


def parse_charge(raw: str | None) -> int | None:
    """Read bounded signed magnitudes; repeated signs and zero remain unresolved."""
    if raw is None:
        return None
    raw = raw.replace('–', '-').replace('—', '-').replace('−', '-')
    match = re.fullmatch(r'([+-])([1-9]\d{0,5})?|([1-9]\d{0,5})([+-])', raw)
    if match is None:
        return None
    sign = match.group(1) or match.group(4)
    magnitude = int(match.group(2) or match.group(3) or '1')
    return magnitude if sign == '+' else -magnitude


def _charge_suffix(text: str, start: int) -> tuple[str | None, int | None, int | None, bool | None]:
    """Retain a complete adjacent charge assertion without interpreting magnitude."""
    start = _skip_space(text, start)
    if start == len(text):
        return None, None, None, None
    if text[start].isdigit():
        match = _CHARGE_DIGITS.match(text, start)
        if match is None:
            return None, None, None, None
        raw = match.group()
        return raw, start, start + len(raw), parse_charge(raw) is not None
    if text[start] not in '+-–—':
        return None, None, None, None
    end = start + 1
    while end < len(text) and text[end] in '+-–—':
        end += 1
    digits_start = end
    while end < len(text) and text[end].isdigit():
        end += 1
    # A directly adjacent decimal remains a measurement field, including bad precision.
    if end > digits_start and end < len(text) and text[end] == '.' and _mass_boundary(text, digits_start):
        end = digits_start
    raw = text[start:end]
    return raw, start, end, parse_charge(raw) is not None


def _charge_ends_formula(text: str, start: int, source: str) -> bool:
    """Require an analytical delimiter after the complete charge assertion."""
    _, _, charge_end, _ = _charge_suffix(text, start)
    if charge_end is None:
        return False
    following = _skip_space(text, charge_end)
    return (following == len(text) or text[following] in ':;,='
            or _ION_START.match(text, following) is not None or _mass_boundary(text, following)
            or _FIELD.match(text, following) is not None
            or (text[following] == '.' and _period_boundary(text, following, source))
            or (following > charge_end and _field_follows(text, following, source)))


def _candidate(text: str, start: int, source: str, header) -> FormulaCandidate | None:
    """Read to an analytical delimiter, retaining unsupported continuations."""
    group_ends = {}
    groups = []
    for index in range(start, len(text)):
        character = text[index]
        if character in '[(':
            groups.append((character, index))
        elif character in '])':
            opening = '[' if character == ']' else '('
            if groups and groups[-1][0] == opening:
                _, group_start = groups.pop()
                group_ends[group_start] = index + 1
            else:
                groups.clear()
    literal_end = _literal_formula_end(text, start) if source == 'hrms' else None
    end = start
    while end < len(text):
        if literal_end is not None and end == literal_end:
            if _charge_ends_formula(text, end, source):
                break
            # The bracket is only a prefix of this complete unsupported token.
            literal_end = None
        character = text[end]
        if character in ':;,=':
            break
        if source == 'ea' and character == '(' and _EA_FIELD.match(text, end):
            break
        if character in '[(' and _ION_START.match(text, end) and not (end == start and literal_end):
            break
        if character in '[(':
            group_end = group_ends.get(end)
            if group_end is not None:
                end = group_end
                continue
        if character == '•' and end + 1 < len(text) and text[end + 1] in '+-–—':
            end += 1
            continue
        if character in '+-–—':
            _, _, charge_end, _ = _charge_suffix(text, end)
            assert charge_end is not None
            if _charge_ends_formula(text, end, source):
                break
            end = charge_end
            continue
        if character == '.' and _period_boundary(text, end, source):
            break
        if character.isspace():
            following_start = _skip_space(text, end)
            if _field_follows(text, following_start, source):
                break
            end = following_start
            continue
        end += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if end == start:
        return None
    raw = text[start:end]
    grammar = _FORMULA if source == 'hrms' else _EA_FORMULA
    formula = raw[1:-1] if literal_end == end else raw
    supported = grammar.fullmatch(raw) is not None or _simple_carbon_formula(formula)
    if source == 'ea' and not supported and not re.search(r'\s', raw):
        policy = parse_ea_formula(raw)
        supported = policy.status == 'accepted' and (
            bool(policy.explicit_components)
            or any(value.denominator != 1 for value in policy.full_counts.values())
        )
    refusal, detail = (None, None) if supported else classify_refusal(raw, source)
    charge_raw, charge_start, charge_end, charge_supported = _charge_suffix(text, end)
    charge = charge_raw[0] if charge_raw else None
    if charge in ('–', '—'):
        charge = '-'
    return FormulaCandidate(
        raw, start, end, source, header.start(), header.end(),
        formula if supported else None,
        None if supported else _REFUSAL_REASONS[refusal].format(detail),
        charge, charge_raw, charge_start, charge_end, charge_supported, refusal, detail,
    )


def find_hrms_formula_candidate(text: str) -> FormulaCandidate | None:
    """Retain the first owned candidate, including unsupported prefixes/suffixes."""
    masked = _mask_ion_brackets(text)
    headers = iter(_INSTRUMENT.finditer(masked))
    boundaries = iter(_OTHER_ANALYSIS.finditer(masked))
    cues = iter(_CALCULATED.finditer(text))
    next_header = next(headers, None)
    next_boundary = next(boundaries, None)
    next_cue = next(cues, None)
    cue_end = 0
    header = None
    boundary_end = 0
    for seed in _SEED.finditer(masked):
        while next_cue is not None and next_cue.start() <= seed.start():
            cue_end = next_cue.end()
            next_cue = next(cues, None)
        if seed.start() < cue_end:
            continue
        if _INSTRUMENT_LABEL.fullmatch(seed.group()):
            continue
        while next_header and next_header.end() <= seed.start():
            header = next_header
            next_header = next(headers, None)
        while next_boundary and next_boundary.end() <= seed.start():
            boundary_end = next_boundary.end()
            next_boundary = next(boundaries, None)
        if header is not None and header.start() >= boundary_end:
            start = seed.start()
            labels = list(_CALCULATED.finditer(text, header.end(), start))
            if labels:
                cue_end = _skip_space(text, labels[-1].end())
                if cue_end < start and text[cue_end] in ':=':
                    cue_end = _skip_space(text, cue_end + 1)
                prefix_start = _skip_ion_labels(text, cue_end, start)
                gap = text[prefix_start:start]
                if not gap or re.fullmatch(r'[0-9\s+./\-–—]*|(?:[A-Z][a-z]?\d*\s*)+|[^\s]+', gap):
                    start = prefix_start
            else:
                header_end = _instrument_closing_end(text, header)
                while start > header_end and not text[start - 1].isspace() and text[start - 1] not in ':;,':
                    start -= 1
                group_end = balanced_group_end(text, start) if text[start] == '(' else None
                if group_end is not None and group_end > seed.start():
                    if group_end == len(text) or not text[group_end].isalnum():
                        # A parenthesis enclosing the whole declaration is not part of it.
                        return _candidate(text[:group_end - 1], start + 1, 'hrms', header)
                    # Group notation such as '(C2H5)2O' keeps its parenthesis and no mass prefix.
                else:
                    prefix_end = start
                    while prefix_end > header_end and text[prefix_end - 1].isspace():
                        prefix_end -= 1
                    prefix_start = prefix_end
                    while prefix_start > header_end and not text[prefix_start - 1].isspace() and text[prefix_start - 1] not in ':;,':
                        prefix_start -= 1
                    # A sentence-final measurement such as 'MH+, 165.0912.' is not a multiplier.
                    if re.fullmatch(r'[+\-–—]?[0-9./]*[0-9]', text[prefix_start:prefix_end]):
                        start = prefix_start
            return _candidate(text, start, 'hrms', header)
    return None


def find_ea_formula_candidate(text: str) -> FormulaCandidate | None:
    """Retain the complete declaration after the existing EA header."""
    header = _EA_HEADER.search(text)
    if header is None:
        return None
    return _candidate(text, header.end(), 'ea', header)


def protect_ea_component_separators(text: str) -> str:
    """Preserve EA-owned dot components before length-changing normalization."""
    candidate = find_ea_formula_candidate(text)
    if candidate is None or not re.search(r'[•ꞏ]', candidate.raw):
        return text
    token = re.sub(r'[•ꞏ]', '·', candidate.raw)
    return text[:candidate.start] + token + text[candidate.end:]


def find_hrms_formula(text: str) -> HRMSFormulaMatch | None:
    """Compatible supported-match adapter; never return a candidate's suffix."""
    candidate = find_hrms_formula_candidate(text)
    if candidate is None or candidate.formula is None:
        return None
    return HRMSFormulaMatch(
        candidate.formula, candidate.header_start, candidate.header_end,
        candidate.start, candidate.end,
    )
