"""Pure compound-identifier and source-name boundary recognition."""

from __future__ import annotations

from bisect import bisect_left

import regex

from checksi.analysis.hrms_fields import _CALCULATED, _STATE_WORD
from checksi.assessment import ANALYTICAL_MARKER, SUPPORTED_PROTON_HEADER, analytical_excerpt
from checksi.utils.text_processing import replace_dash


# Both terminal-field checks and the resolver use this established grammar.
_IDENTIFIER_FIELDS = (
    (r"\d+['`#@]{0,2}[a-z'`\"]+[-'`]{0,2}[0-9\-]*", r"(?:,\s*[^)]*)?", "alphanumeric_id_match_count"),
    (r"-?\d+(?:\.\d+)?['`#@]{0,2}", r"(?:,\s*(?!\d)[^)]*)?", "numeric_id_match_count"),
    (r"[a-zA-Z]{1,3}['`#@]{0,2}-?\d+(?:\.\d+)?[a-zA-Z]{0,2}", r"(?:,\s*[^)]*)?", "general_id_match_count"),
    (r"\d+-(?:[A-Z]+|\d+[A-Z]+)", r"(?:,\s*(?!\d)[^)]*)?", "numeric_prefix_code_count"),
    (r"[A-Z]", "", "single_uppercase_id_match_count"),
)
IDENTIFIER_PATTERNS = tuple((r"\((" + base + tail + r")\)", key) for base, tail, key in _IDENTIFIER_FIELDS)
_IDENTIFIER_BASES = tuple(regex.compile(base) for base, _, _ in _IDENTIFIER_FIELDS)
FORBIDDEN_IDENTIFIER_UNITS = (" mg", " g", " ml", " mL", " L", " mol", "equiv", " mmol", " eq")
_SOURCE_NAME_LIMIT = 16_384
# Numeric shifts use the ordinary NMR counter's integer/decimal/range grammar.
_SIGNAL_SHIFT = r'-?\d+(?:\.\d+)?(?:\s*[-–—]\s*-?\d+(?:\.\d+)?)?'
_PROTON_SIGNAL = regex.compile(_SIGNAL_SHIFT + r'\s*\(([^()]*)\)')
_H_INTEGRAL = regex.compile(r'\d+(?:\.\d+)?\s*[HН]')
_NUMERIC_FRAGMENT = regex.compile(
    _SIGNAL_SHIFT + r'\s*\(|-?\d+\.\d+(?=[\s(,;]|$)'
)
_DATA_TAIL = regex.compile(
    r'\b(?:calcd|calculated|found|NMR|MHz|Hz|m/z)\b|δ|\bJ\s*='
    r'|\[\s*M(?:[+−–—\-\]]|\b)|\b(?:FT-)?IR\s*(?:\(|:)'
    r'|\bcm\s*[-−–—]\s*1\b|(?:^|[;,:]\s*)[A-Z][a-z]?\s*,\s*\d'
    r'|\(\s*\d+\s*C\s*\)|(?<![\w-])\d+(?:\.\d+)?\s*H(?=[\s,;)]|$)', regex.I,
)
_DECIMAL_DATA = regex.compile(r'(?<![\w.])\d+\.\d+(?=[\s,;]|$)')
_BRACKET_DESCRIPTOR = regex.compile(r'^\(\s*(?:(?:br\.?\s*)?[A-Za-z]{1,4}|AB-q)?(?=\s*[,);]|$)')
_STEREO_BRACKET = regex.compile(r'^\(\s*\d*[RSEZ]{1,2}\*?(?:\s*,\s*\d*[RSEZ]{1,2}\*?)*\s*\)')
_TRAILING_SHIFT = regex.compile(r'(?:^|[\s,;:])' + _SIGNAL_SHIFT + r'\s*$')
_FORMULA_ATOM = r'(?:[A-Z][a-z]?+(?:\d++(?:\.\d++)?+)?+|\[\d++[A-Z][a-z]?+\](?:\d++(?:\.\d++)?+)?+)'
_FORMULA_FRAGMENT = regex.compile(_FORMULA_ATOM + r'(?:' + _FORMULA_ATOM + r'|[()[\]·.0-9])*+(?:[+−–—-]++\d*+)?+')
_FORMULA_ATOMS = regex.compile(_FORMULA_ATOM)
_FormulaContext = tuple[bool, int, bool]
_CONTENTS_FENCE = regex.compile(r'^(?:table of contents|contents)$|(?:\.{3,}|-{3,})\s*\d+\s*$', regex.I)
_MASS_CONTINUATION = regex.compile(
    r'(?:[+−–—-]\s*)?\d+(?:\.\d+)?\s*[;,]?|[+−–—-]|(?:found|calcd|calculated)\s*[.:;]?', regex.I
)
_NAME_FOLLOWUP = regex.compile(
    r'(?:(?:colou?rless|(?:pale\s+)?yellow)\s+(?:oil|solid|crystals?)\b|yield\b|route\b'
    r'|product\s+\S+\s+was\s+(?:synthesized|prepared)\b)', regex.I,
)


def balanced_name(text: str) -> bool:
    """Require paired and properly nested brackets in an isolated name."""
    stack = []
    for char in text:
        if char in "([{":
            stack.append(char)
        elif char in ")]}":
            if not stack or stack.pop() != {")": "(", "]": "[", "}": "{"}[char]:
                return False
    return not stack


def balanced_identifier_starts(text: str) -> set[int]:
    """Index balanced prefixes in one pass for repeated single-letter matches."""
    starts = set()
    stack = []
    malformed = False
    for index, char in enumerate(text):
        if char == '(' and not stack and not malformed:
            starts.add(index)
        if char in '([{':
            stack.append(char)
        elif char in ')]}':
            if not stack or stack.pop() != {')': '(', ']': '[', '}': '{'}[char]:
                malformed = True
    return starts


def identifier_allowed(text: str, start: int, end: int, identifier: str, stat_key: str,
                       *, prefix_balanced: bool | None = None) -> bool:
    """Exclude quantities and embedded single-letter stereodescriptors."""
    if any(unit in identifier for unit in FORBIDDEN_IDENTIFIER_UNITS):
        return False
    if stat_key == "single_uppercase_id_match_count":
        if prefix_balanced is None:
            prefix_balanced = balanced_name(text[:start])
        return not text.startswith(("-", "–", "—"), end) and prefix_balanced
    return True


def terminal_identifier(text: str) -> tuple[int, int] | None:
    """Return an existing-grammar terminal identifier span, allowing '.' or ':'."""
    if len(text) > _SOURCE_NAME_LIMIT or len(text.encode("utf-8")) > _SOURCE_NAME_LIMIT:
        return None
    end = len(text.rstrip(" .:\u200b\t\r\n"))
    if not end or text[end - 1] != ')':
        return None
    close = end - 1
    # Identifier fields cannot contain ')'. Separate the comma tail so the
    # permissive legacy tail grammar never backtracks across repeated '(' starts.
    region = text.rfind(')', 0, close) + 1
    openings = [index for index in range(region, close) if text[index] == '(']
    commas = [index for index in range(region, close) if text[index] == ',']
    # The legacy optional whitespace may backtrack to zero characters before
    # its digit lookahead; preserve that grammar for space-led comma tails.
    numeric_tails = {comma for comma in commas if comma + 1 < close and text[comma + 1].isdecimal()}
    last_forbidden = max((text.rfind(unit, region, close) for unit in FORBIDDEN_IDENTIFIER_UNITS), default=-1)
    balanced_starts = None
    for priority, pattern in enumerate(_IDENTIFIER_BASES):
        for start in openings:
            if last_forbidden >= start + 1:
                continue
            comma_index = bisect_left(commas, start)
            comma = commas[comma_index] if comma_index < len(commas) else close
            if pattern.fullmatch(text, start + 1, comma) is None:
                continue
            if comma < close:
                if priority == 4:
                    continue
                if priority in (1, 3) and comma in numeric_tails:
                    continue
            if priority == 4:
                if balanced_starts is None:
                    balanced_starts = balanced_identifier_starts(text)
                if start not in balanced_starts or text.startswith(("-", "–", "—"), end):
                    continue
            return start, end
    return None


def _name_source_fence(text: str) -> bool:
    """Recognize report findings or a leading mass label without analytical data."""
    if text.startswith(("⚠", "❗")):
        return True
    marker = ANALYTICAL_MARKER.match(text)
    return marker is not None and marker.lastgroup == "ms" and analytical_excerpt(text) is None


def _signal_source_text(text: str) -> str:
    """Normalize spectral marker/sign variants for recognition, preserving source."""
    text = replace_dash(text).strip()
    if text.startswith('δ'):
        text = text[1:].lstrip(' :=')
    return text


def _proton_signal_line(text: str) -> bool:
    """Recognize only complete shift/descriptor records with whole H integrals."""
    if len(text.encode("utf-8")) > _SOURCE_NAME_LIMIT:
        return False
    text = _signal_source_text(text)
    end = 0
    try:
        for match in _PROTON_SIGNAL.finditer(text, timeout=0.05):
            if text[end:match.start()].strip(' ,;'):
                return False
            if not any(_H_INTEGRAL.fullmatch(field.strip()) for field in match.group(1).split(',')):
                return False
            end = match.end()
    except TimeoutError:
        return False
    return end > 0 and not text[end:].strip(' ,;.')


def _formula_fragment(text: str) -> bool:
    """Recognize bounded molecular notation without deciding scientific validity."""
    if len(text) > _SOURCE_NAME_LIMIT or text in {'R', 'S', 'E', 'Z', 'RS', 'SR'}:
        return False
    if _FORMULA_FRAGMENT.fullmatch(text) is None:
        return False
    return any(char.isdecimal() for char in text) or len(_FORMULA_ATOMS.findall(text)) > 1


def _formula_cue_owner(text: str) -> bool | None:
    """Require the latest analytical source to own an unfinished declaration."""
    if len(text.encode('utf-8')) > _SOURCE_NAME_LIMIT:
        return None
    marker = None
    for marker in ANALYTICAL_MARKER.finditer(text):
        pass
    if marker is None or marker.lastgroup not in {'ea', 'ms'}:
        return False
    cue = None
    # A trailing space lets the shared cue grammar recognize a terminal 'for'.
    source = text + ' '
    for cue in _CALCULATED.finditer(source, marker.start()):
        pass
    if cue is None:
        return False
    remainder = source[cue.end():].strip(' :=')
    return not remainder or _formula_fragment(remainder)


def _formula_tail_context(parts: list[str], index: int, cache: dict[int, _FormulaContext]) -> bool | None:
    """Prove formula/state continuations once per source window; None is its bound."""
    trail = []
    total = 0
    cursor = index
    while cursor >= 0:
        if cursor in cache:
            context = cache[cursor]
            break
        part = parts[cursor]
        if len(part) > _SOURCE_NAME_LIMIT:
            return None
        formula = _formula_fragment(part)
        if not formula and _STATE_WORD.fullmatch(part) is None:
            if not trail:
                return False
            owner = _formula_cue_owner(part)
            if owner is None:
                return None
            context = (owner, 0, False)
            break
        size = len(part.encode('utf-8')) + 1
        total += size
        if total > _SOURCE_NAME_LIMIT:
            return None
        trail.append((cursor, formula, size))
        cursor -= 1
    else:
        context = (False, 0, False)
    owner, size, seen_formula = context
    for position, formula, part_size in reversed(trail):
        size += part_size
        if size > _SOURCE_NAME_LIMIT:
            return None
        seen_formula = seen_formula or formula
        cache[position] = (owner, size, seen_formula)
    return owner and seen_formula


def _source_analysis_context(parts: list[str], index: int,
                             formula_context: dict[int, _FormulaContext] | None = None) -> bool:
    """Prove analysis directly or across bounded, data-shaped source tails."""
    if index < 0:
        return False
    if analytical_excerpt(parts[index]):
        return True
    if formula_context is None:
        formula_context = {}
    total_bytes = 0
    cursor = index
    proton_only = True
    needs_mass_owner = False
    while cursor >= 0:
        part = parts[cursor]
        if analytical_excerpt(part):
            if needs_mass_owner:
                marker = None
                for marker in ANALYTICAL_MARKER.finditer(part):
                    pass
                return marker is not None and marker.lastgroup in {'ea', 'ms'}
            return not proton_only or SUPPORTED_PROTON_HEADER.match(part) is not None
        formula = _formula_tail_context(parts, cursor, formula_context)
        if formula is not False:
            return formula is True
        if _proton_signal_line(part):
            pass
        elif _MASS_CONTINUATION.fullmatch(part):
            # Bare charges, masses and an unfinished 'found.' belong to a
            # preceding mass section only; they cannot prove an NMR tail.
            needs_mass_owner = True
            proton_only = False
        elif (not part.rstrip().endswith('.') and not _numeric_name_fragment(part)
              and _DATA_TAIL.search(part)):
            proton_only = False
        elif _bracket_descriptor_fragment(part):
            # A descriptor can be incomplete: require its preceding shift,
            # then continue back to the owning analytical source marker.
            if cursor == 0 or not _TRAILING_SHIFT.search(_signal_source_text(parts[cursor - 1][-_SOURCE_NAME_LIMIT:])):
                return False
            proton_only = False
        else:
            return False
        total_bytes += len(parts[cursor].encode("utf-8")) + 1
        if total_bytes > _SOURCE_NAME_LIMIT:
            return False
        cursor -= 1
    return False


def _data_bearing_name(text: str) -> bool:
    """Keep analytical data and unproven bracket fragments out of name wraps."""
    return (_numeric_name_fragment(text) or _DATA_TAIL.search(text) is not None
            or _DECIMAL_DATA.search(text) is not None or unproven_bracket_fragment(text))


def _bracket_descriptor_fragment(text: str) -> bool:
    """Recognize short signal fields without treating chemical stereo as data."""
    text = text[:_SOURCE_NAME_LIMIT].lstrip()
    return _BRACKET_DESCRIPTOR.match(text) is not None and not _chemical_bracket_prefix(text)


def _chemical_bracket_prefix(text: str) -> bool:
    """Prove stereo or text attached to a complete leading chemical group."""
    if _STEREO_BRACKET.match(text):
        return True
    stack = []
    for index, char in enumerate(text):
        if char in '([{':
            stack.append(char)
        elif char in ')]}':
            if not stack or stack.pop() != {')': '(', ']': '[', '}': '{'}[char]:
                return False
            if not stack:
                following = text[index + 1:index + 2]
                return bool(following) and (following.isalnum() or following in '-–—')
    return False


def unproven_bracket_fragment(text: str) -> bool:
    """Refuse unattached bracket fragments even when no descriptor is known."""
    text = text[:_SOURCE_NAME_LIMIT].lstrip()
    return text.startswith(('(', '[')) and not _chemical_bracket_prefix(text)


def _numeric_name_fragment(text: str) -> bool:
    """Refuse ambiguous signal-shaped fragments instead of wrapping them as names."""
    text = _signal_source_text(text[:_SOURCE_NAME_LIMIT])
    # A leading plus still identifies numeric source even when the complete
    # record grammar cannot prove it as a continuation.
    if text.startswith('+'):
        text = text[1:].lstrip()
    try:
        return _NUMERIC_FRAGMENT.match(text, timeout=0.05) is not None
    except TimeoutError:
        return True


def compound_name_start(parts: list[str], candidate_index: int) -> int | None:
    """Find a name's source-part start after analysis and before a proton header.

    Parts use the caller's shared body normalization but retain native DOCX
    paragraph/PDF line boundaries. Wrapped name parts are kept together; an
    earlier identifier prevents a property/procedure label from becoming a name.
    """
    following_index = candidate_index + 1
    # An explicit property/preparation paragraph can sit between the chemical
    # heading and its spectrum. It cannot turn its own quantity/route ID into
    # a new heading or consume a paragraph containing analytical data.
    while (following_index < min(len(parts), candidate_index + 4)
           and _NAME_FOLLOWUP.match(parts[following_index][:_SOURCE_NAME_LIMIT])
           and analytical_excerpt(parts[following_index]) is None):
        following_index += 1
    following = " ".join(part[:_SOURCE_NAME_LIMIT] for part in parts[following_index:following_index + 4]).lstrip()
    # Some source documents put a separate sentence dot before the next header.
    if following.startswith(". "):
        following = following[2:].lstrip()
    if not SUPPORTED_PROTON_HEADER.match(following):
        return None
    candidate = parts[candidate_index]
    candidate_start = candidate_index
    identifier = terminal_identifier(candidate)
    # PDF line wrapping can split a final identifier's comma annotation, as
    # '(1.1,' / 'correct)'. Rejoin only this bounded, incomplete terminal field.
    if identifier is None and candidate.rstrip(' .:').endswith(')'):
        while candidate_start > max(0, candidate_index - 3):
            candidate_start -= 1
            candidate = parts[candidate_start] + ' ' + candidate
            if len(candidate.encode('utf-8')) > _SOURCE_NAME_LIMIT:
                return None
            identifier = terminal_identifier(candidate)
            if identifier is not None:
                break
    if (identifier is None or analytical_excerpt(candidate)
            or _data_bearing_name(candidate[:identifier[0]])):
        return None
    formula_context = {}
    if _formula_tail_context(parts, candidate_index, formula_context) is not False:
        return None
    start = candidate_start
    name_bytes = len(candidate.encode("utf-8"))
    while start:
        previous = parts[start - 1]
        context_index = start - 1
        # Report findings and data-free mass labels are source fences, not names.
        while context_index >= 0 and _name_source_fence(parts[context_index]):
            context_index -= 1
        analysis_context = _source_analysis_context(parts, context_index, formula_context)
        if not analysis_context and _formula_tail_context(parts, context_index, formula_context) is None:
            return None
        if context_index != start - 1:
            if not analysis_context:
                return None
            previous = parts[context_index]
        if analysis_context:
            name = " ".join(parts[start:candidate_start] + [candidate[:identifier[0]]]).strip()
            return start if name and balanced_name(name) else None
        if _CONTENTS_FENCE.search(previous):
            name = " ".join(parts[start:candidate_start] + [candidate[:identifier[0]]]).strip()
            return start if name and balanced_name(name) else None
        if _data_bearing_name(previous):
            return None
        # A finished source paragraph or another compound label is not a name
        # continuation. This also protects quantity/yield/procedure paragraphs.
        if previous.rstrip().endswith((".", ":", ";", "!", "?")) or terminal_identifier(previous):
            return None
        # A multiword source fragment needs chemical punctuation to establish
        # a wrap. Plain discussion/report prose remains in the refused context.
        if len(previous.split()) > 1 and not any(char in previous for char in "-()[]"):
            return None
        name_bytes += len(previous.encode("utf-8")) + 1
        if name_bytes > _SOURCE_NAME_LIMIT:
            return None
        start -= 1
    return None


def compound_name_starts(parts: list[str]) -> set[int]:
    """Return proven boundaries using cheap header gates before bounded scans."""
    return {start for index in range(len(parts))
            if (start := compound_name_start(parts, index)) is not None}
