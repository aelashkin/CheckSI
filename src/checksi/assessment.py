"""Coverage of detected descriptions, independent of findings and export status."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Literal, NamedTuple, TypedDict

from checksi.analysis.hrms_fields import HRMS_INSTRUMENT_PATTERN, protect_ea_component_separators
from checksi.utils.text_processing import DASH_REPLACEMENTS, replace_with_offsets


class TableLocation(TypedDict):
    table_1based: int
    row_1based: int
    column_1based: int


class SourceLocation(TypedDict, total=False):
    kind: Literal["document", "paragraph", "docx_table"]
    paragraph_1based: int
    start_offset: int
    end_offset: int
    table_path: list[TableLocation]


class UnassessedDescription(TypedDict):
    source: SourceLocation
    excerpt: str
    reason: str


class Assessment(TypedDict):
    status: Literal["complete", "partial", "none"]
    assessed_descriptions: int
    unassessed: list[UnassessedDescription]


ASSESSMENT_LABELS = {"complete": "Assessed", "partial": "Partly assessed", "none": "Nothing assessed"}


# This is the existing controller admission pattern, without grammar expansion.
SUPPORTED_PROTON_HEADER = re.compile(
    r'1H[\s-]?NMR \([^)]*(?:\([^)]*\)[^)]*)*, \d+(?:\.\d+)? MHz\)(: δ| δ)'
    r'|1H[\s-]?NMR \(\d+(?:\.\d+)? MHz, [^)]*(?:\([^)]*\)[^)]*)*\)(: δ| δ)'
)
PROTON_MARKER = re.compile(r'\b1H[\s-]?NMR\b', re.IGNORECASE)
ANALYTICAL_MARKER = re.compile(
    r'\b(?P<nucleus>1H|13C|19F|11B|31P|29Si|15N|2H)\s*(?:\{[^}]*\})?[\s-]?NMR\b'
    r'|\b(?P<ea>Anal\.?\s*calcd\.?)'
    rf'|\b(?P<ms>(?:HR)?(?:ESI|APCI)(?:-?MS)?|{HRMS_INSTRUMENT_PATTERN})\b',
    re.IGNORECASE,
)
_MASS_DATA = re.compile(
    r'(?-i:\bC\d*(?:[A-Z][a-z]?|\[)|\[\d+C\]\d*[A-Z])|\b(?:calcd|calculated)\b'
    r'|\b(?:found|m/z)\s*[:;,]?\s*\d', re.IGNORECASE)
_SENTENCE_END = re.compile(r'(?<!calcd)(?<!calc)\.\s+|[!?]\s+', re.IGNORECASE)
# A period directly after an ion label such as [M+H]+ or M+ continues the sentence.
_ION_LABEL_END = re.compile(r'(?:\[[^\[\]]{1,30}\]|\bM)\s*\d*[+\-−–—•·]+$')
_DATA_FRAGMENT = re.compile(
    r'^\s*[,(;:\s]*(?:\d|δ|[\[(]\s*M\b)'
    r'|\b\d+(?:\.\d+)?\s*(?:H|C|Hz|MHz)\b'
    r'|(?:^|[;,:]\s*)[A-Z][a-z]?\s*,\s*\d', re.IGNORECASE)
_WORD = re.compile(r'[\w)\]]')
EXCERPT_LIMIT = 600
FORMULA_NOTATION_NOT_SUPPORTED = "formula_notation_not_supported"
# After EA-owned separator protection, match the controller's dash/BArF input.
_MASS_TEXT_REPLACEMENTS = (*DASH_REPLACEMENTS, (re.compile(r"\bBArF\b"), "C32H12BF24"))
REASON_LABELS = {
    "unsupported_proton_header": "Unsupported proton-NMR header",
    "no_supported_proton_header": "Analytical data without a supported proton-NMR header",
    "additional_description_in_paragraph": "Additional description in an already assessed paragraph",
    "additional_analysis_section": "Additional analytical section not consumed by the checker",
    "docx_table_not_supported": "Analytical table text is outside the supported extraction scope",
    "docx_table_structure_not_supported": "DOCX contains tables outside the supported table inspection structure",
    "docx_content_not_supported": "Analytical text outside the supported DOCX extraction structure",
    "docx_source_scan_limit": "DOCX source inspection limit reached; remaining content is unassessed",
    "docx_source_inspection_failed": "DOCX source inspection did not finish; unsupported content remains unassessed",
    "docx_deleted_content_not_supported": "Deleted revision text is retained as unassessed source evidence",
    FORMULA_NOTATION_NOT_SUPPORTED: "Formula notation not checked by CheckSI",
    "no_recognized_descriptions": "No supported analytical descriptions were recognized",
    "scan_incomplete": "Description scanning did not finish",
}


class _SectionMarker(NamedTuple):
    kind: str
    start: int
    end: int


def _sentence_end(text: str, start: int, end: int) -> int:
    """Return the first sentence end in ``text[start:end]``, or ``end``."""
    for match in _SENTENCE_END.finditer(text, start, end):
        if not (text[match.start()] == "."
                and _ION_LABEL_END.search(text, max(start, match.start() - 40), match.start())):
            return match.start()
    return end


def _section_markers(text: str) -> list[_SectionMarker]:
    """Align candidate identities while grouping aliases in one instrument header."""
    markers = []
    for match in ANALYTICAL_MARKER.finditer(text):
        kind = match.group("nucleus").upper() if match.group("nucleus") else match.lastgroup
        marker = _SectionMarker(kind, match.start(), match.end())
        if (kind == "ms" and markers and markers[-1].kind == "ms"
                and not _MASS_DATA.search(text, markers[-1].end, marker.start)):
            markers[-1] = markers[-1]._replace(end=marker.end)
        else:
            markers.append(marker)
    retained = []
    for index, marker in enumerate(markers):
        end = _sentence_end(text, marker.end,
                            markers[index + 1].start if index + 1 < len(markers) else len(text))
        if marker.kind != "ms" or _MASS_DATA.search(text, marker.end, end):
            retained.append(marker)
    return retained


def analytical_excerpt(
    text: str, *, unextracted_spans: list[tuple[int, int]] | None = None,
) -> str | None:
    """Start a bounded omission excerpt at its first analytical candidate.

    With ``unextracted_spans``, a candidate qualifies when it loses header
    text, data, or any text that extracted words follow inside the candidate.
    """
    markers = _section_markers(text)
    extracted = text
    if unextracted_spans:
        characters = list(text)
        for start, stop in unextracted_spans:
            characters[start:stop] = " " * (stop - start)
        extracted = "".join(characters)
    for index, marker in enumerate(markers):
        if unextracted_spans is None:
            return text[marker.start:marker.start + EXCERPT_LIMIT]
        end = markers[index + 1].start if index + 1 < len(markers) else len(text)
        for start, stop in unextracted_spans:
            if start >= end or stop <= marker.start:
                continue
            fragment = text[max(start, marker.start):min(stop, end)]
            if (start < marker.end or _MASS_DATA.search(fragment) or _DATA_FRAGMENT.search(fragment)
                    or (fragment.strip() and _WORD.search(extracted, stop, end))):
                return text[marker.start:marker.start + EXCERPT_LIMIT]
    return None


def unassessed_description(
    text: str, source: SourceLocation, reason: str,
) -> UnassessedDescription:
    """Retain a bounded excerpt with its location and coverage reason."""
    return {"source": deepcopy(source), "excerpt": text.strip()[:EXCERPT_LIMIT], "reason": reason}


def paragraph_omissions(text: str, paragraph_1based: int) -> list[UnassessedDescription]:
    """Account for each proton candidate, including candidates inside one paragraph."""
    omissions = []
    protons = list(PROTON_MARKER.finditer(text))
    accepted = SUPPORTED_PROTON_HEADER.search(text)
    sections = _section_markers(text)
    selected_spans = {}
    if accepted is not None:
        from checksi.analysis.hrms_fields import find_ea_formula_candidate, find_hrms_formula_candidate
        from checksi.analysis.nmr import _match_nmr_section

        for kind in {section.kind for section in sections} - {"ea", "ms"}:
            match, _ = _match_nmr_section(text, "29Si" if kind == "29SI" else kind)
            if match is not None:
                selected_spans[kind] = match.span()
        # Select on the text the EA/HRMS checks read; report paragraph offsets.
        mass_text, origin = replace_with_offsets(protect_ea_component_separators(text), _MASS_TEXT_REPLACEMENTS)
        ea = find_ea_formula_candidate(mass_text)
        ms = find_hrms_formula_candidate(mass_text)
        if ea is not None:
            selected_spans["ea"] = (origin[ea.header_start], origin[ea.end])
        if ms is not None:
            selected_spans["ms"] = (origin[ms.header_start], origin[ms.end])

    def omit(start: int, end: int, reason: str) -> None:
        omissions.append(unassessed_description(text[start:end], {
            "kind": "paragraph", "paragraph_1based": paragraph_1based,
            "start_offset": start, "end_offset": end,
        }, reason))

    prefix_end = protons[0].start() if protons else len(text)
    prefix_marker = next((marker for marker in sections if marker.start < prefix_end), None)
    if prefix_marker and accepted is None:
        omit(prefix_marker.start, prefix_end, "no_supported_proton_header")
    for index, marker in enumerate(protons):
        end = protons[index + 1].start() if index + 1 < len(protons) else len(text)
        if marker.start() == selected_spans.get("1H", (-1, -1))[0]:
            continue
        # A later analyzer-selected section is assessed independently of this
        # unsupported proton block and must not be included in its omission.
        end = min([end, *[start for start, _ in selected_spans.values()
                          if marker.end() <= start < end]])
        reason = ("additional_description_in_paragraph"
                  if SUPPORTED_PROTON_HEADER.match(text, marker.start())
                  else "unsupported_proton_header")
        omit(marker.start(), end, reason)
    if accepted is not None:
        for section_index, section in enumerate(sections):
            if section.kind == "1H":
                continue
            selected = selected_spans.get(section.kind)
            if selected is not None and section.start <= selected[0] < section.end:
                continue
            if any(item["source"]["start_offset"] <= section.start < item["source"]["end_offset"]
                   for item in omissions):
                # An unassessed whole proton-description record already
                # retains this nested section; avoid duplicate evidence.
                continue
            end = sections[section_index + 1].start if section_index + 1 < len(sections) else len(text)
            if section.kind not in ("ea", "ms"):
                local_match, _ = _match_nmr_section(text[section.start:end],
                                                   "29Si" if section.kind == "29SI" else section.kind)
                if local_match is not None and local_match.start() == 0:
                    end = section.start + local_match.end()
            omit(section.start, end, "additional_analysis_section")
    omissions.sort(key=lambda item: item["source"]["start_offset"])
    return omissions


def build_assessment(
    assessed: int,
    omissions: list[UnassessedDescription] | tuple[UnassessedDescription, ...] = (),
    *,
    scan_complete: bool = True,
) -> Assessment:
    """Return a run-owned coverage snapshot without changing chemistry counters."""
    retained = deepcopy(list(omissions))
    if not scan_complete:
        retained.append(unassessed_description("", {"kind": "document"}, "scan_incomplete"))
    elif not assessed and not retained:
        retained.append(unassessed_description("", {"kind": "document"}, "no_recognized_descriptions"))
    return {
        "status": "none" if not assessed else "partial" if retained else "complete",
        "assessed_descriptions": assessed,
        "unassessed": retained,
    }


def source_label(source: SourceLocation) -> str:
    """Describe an extracted paragraph or a nested DOCX cell for readers."""
    if source["kind"] == "paragraph":
        return f"Extracted paragraph {source['paragraph_1based']}"
    if source["kind"] == "docx_table":
        path = " / ".join(
            f"table {step['table_1based']}, row {step['row_1based']}, column {step['column_1based']}"
            for step in source["table_path"]
        )
        paragraph = source.get("paragraph_1based")
        return f"DOCX {path}" + (f", paragraph {paragraph}" if paragraph else "")
    return "Document"


def run_completion_message(analysis: dict, report: dict) -> str:
    """Return the terminal progress message; cancellation precedes partial export."""
    if analysis.get("aborted") or report.get("aborted"):
        return "Run cancelled"
    return "Run completed with partial export" if report.get("partial") else "Run completed"
