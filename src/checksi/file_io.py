# Standard library imports
import os
import re
import json
from pathlib import Path

# Third-party imports
import fitz  # PyMuPDF
import docx
from docx.oxml.ns import qn
from docx.table import _Cell

from checksi.assessment import (
    ANALYTICAL_MARKER, SUPPORTED_PROTON_HEADER, UnassessedDescription,
    analytical_excerpt, unassessed_description,
)
from checksi.utils.name_boundaries import (
    balanced_name, compound_name_start, compound_name_starts, terminal_identifier,
    unproven_bracket_fragment,
)
from checksi.analysis.hrms_fields import find_ea_formula_candidate, find_hrms_formula_candidate


# Symbol-font glyphs: PyMuPDF reports Latin letters (legacy Adobe Symbol fonts:
# "Symbol", "SymbolMT", "ABCDEF+Symbol,Bold" ...) or private-use code points
# U+F041-U+F07A for Greek letters. Map them 1:1 so offsets are unchanged. Latin
# text in other fonts, including Unicode fonts whose names merely contain
# "Symbol" (AppleSymbols, SegoeUISymbol, STIXSymbols ...), is never touched.
_SYMBOL_LATIN = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_SYMBOL_GREEK = "αβχδεφγηιϕκλμνοπθρστυϖωξψζΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖ"
_SYMBOL_FONT_MAP = str.maketrans(_SYMBOL_LATIN, _SYMBOL_GREEK)
_SYMBOL_PUA_MAP = str.maketrans({chr(0xF000 + ord(latin)): greek
                                 for latin, greek in zip(_SYMBOL_LATIN, _SYMBOL_GREEK)})


_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")
_LEGACY_SYMBOL_FONT = re.compile(
    r"^symbol(?:mt|ps|std)?(?:[,-](?:bold|italic|bolditalic|oblique|regular))?$"
)


def is_legacy_symbol_font(font: str) -> bool:
    """True for Adobe Symbol (Symbol, SymbolMT, Symbol,Bold, ABCDEF+SymbolMT ...)."""
    name = _SUBSET_PREFIX.sub("", font or "")
    name = re.sub(r"\s+", "", name).lower()
    return bool(_LEGACY_SYMBOL_FONT.match(name))


def symbol_span_text(text: str, font: str) -> str:
    """Return span text with Symbol-encoded Greek letters as Unicode Greek."""
    text = text.translate(_SYMBOL_PUA_MAP)
    if is_legacy_symbol_font(font):
        text = text.translate(_SYMBOL_FONT_MAP)
    return text


def _symbol_block_texts(page, textpage) -> dict[int, tuple[str, str]]:
    """Block number -> (original text, Greek-mapped text) for blocks with Symbol glyphs."""
    changed = {}
    for block in page.get_text("dict", sort=False, textpage=textpage)["blocks"]:
        if block.get("type") != 0:
            continue
        lines = block.get("lines", [])
        original = "".join("".join(span["text"] for span in line["spans"]) + "\n" for line in lines)
        mapped = "".join("".join(symbol_span_text(span["text"], span.get("font", ""))
                                 for span in line["spans"]) + "\n" for line in lines)
        if mapped != original:
            changed[block["number"]] = (original, mapped)
    return changed


class FileIOError(Exception):
    """Base exception for file I/O and text extraction failures."""


class InputFileNotFoundError(FileIOError):
    """Raised when the provided input path does not exist or is not a file."""


class UnsupportedFileTypeError(FileIOError):
    """Raised when the input file type is not supported for text extraction."""


class EmptyTextError(FileIOError):
    """Raised when text extraction succeeds but results in empty text."""


class FileReadError(FileIOError):
    """Raised when a file cannot be opened or parsed."""


def write_dp5q_artifacts(report_parent: Path, index: int,
                         request: dict, result: dict) -> Path:
    """Write opt-in finite JSON/SDF evidence under a new run-owned directory."""
    if type(index) is not int or index < 1:
        raise ValueError("Invalid artifact compound index.")
    payload = {"request": request, "result": result}
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2)
    geometry = result.get("diagnostics", {}).get("geometry_sdf")
    if geometry is not None and not isinstance(geometry, str):
        raise ValueError("Artifact geometry must be SDF text.")
    destination = report_parent / "dp5_artifacts" / f"compound_{index}"
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "assessment.json").write_text(encoded + "\n", encoding="utf-8")
    if geometry is not None:
        (destination / "candidate.sdf").write_text(geometry, encoding="utf-8")
    return destination


def paragraphs_from_input(
    input_path: str,
    *,
    header_margin: float,
    footer_margin: float,
) -> list[str]:
    """Getting text from the file and splitting it into paragraphs"""

    if not input_path or not str(input_path).strip():
        raise InputFileNotFoundError("Input path was not provided.")

    normalized_path = os.path.abspath(os.path.normpath(str(input_path)))
    if not os.path.isfile(normalized_path):
        raise InputFileNotFoundError(f"Input file not found: '{normalized_path}'")

    # Extracting all the text from the file
    lower_path = normalized_path.lower()
    try:
        if lower_path.endswith(".pdf"):
            text = extract_text_from_pdf(
                normalized_path,
                header_margin=header_margin,
                footer_margin=footer_margin,
            )
        elif lower_path.endswith(".docx"):
            _, source_paragraphs = _read_docx_body(normalized_path)
            return _paragraphs_from_docx_body(source_paragraphs, normalized_path)
        else:
            raise UnsupportedFileTypeError(
                f"Unsupported input file type: '{normalized_path}'. Expected .pdf or .docx."
            )
    except FileIOError:
        raise
    except Exception as exc:
        raise FileReadError(f"Failed to read or parse input file: '{normalized_path}'") from exc

    return _paragraphs_from_source_parts(text.splitlines() if text else [], normalized_path)


def _paragraphs_from_docx_body(source_paragraphs: list[str], normalized_path: str) -> list[str]:
    """Apply source-name grouping to native body paragraphs."""
    return _paragraphs_from_source_parts(source_paragraphs, normalized_path)


def _paragraphs_from_source_parts(source_parts: list[str], normalized_path: str) -> list[str]:
    """Keep bounded complete names separate before normalization flattens them."""
    parts = [_normalize_body_text(part).strip() for part in source_parts if part.strip()]
    starts = _source_name_starts(parts)
    boundaries = sorted({0, *starts, len(parts)})
    chunks = ['\n'.join(parts[start:end]) for start, end in zip(boundaries, boundaries[1:])]
    if not any(chunk.strip() for chunk in chunks):
        raise EmptyTextError(f"No extractable text found in file: '{normalized_path}'")
    return [paragraph for chunk in chunks if chunk.strip()
            for paragraph in _paragraphs_from_text(chunk, normalized_path)]


_SOURCE_PROPERTY = re.compile(
    r'(?i)^\s*(?:(?:was\s+)?(?:obtained|isolated|prepared)\b'
    r'|(?:(?:a|an|pale|light|dark)\s+)?(?:colou?rless|white|off-white|yellow|brown|orange|red|green|blue|black)\b'
    r'|(?:oil|solid|crystals?|powder|yield)\b|(?:FT-?IR|IR|m\.?\s*p\.?|Rf)\b)'
)
_INLINE_PROPERTY = re.compile(r'\)\s*(?:[.:;]\s*)?')
_QTOF_HEADER = re.compile(r'\bQ-?TOF\b', re.I)


def _source_property_label(part: str) -> str:
    """Expose an actual identifier before a bounded, clear property suffix."""
    for match in _INLINE_PROPERTY.finditer(part, 0, 16_384):
        candidate = part[:match.start() + 1]
        if terminal_identifier(candidate) and _SOURCE_PROPERTY.match(part[match.end():]):
            return candidate
    return part


def _source_name_starts(parts: list[str]) -> set[int]:
    """Fence physical name headings across bounded appearance/property text.

    This proof view changes no extracted text and grants no resolver identity.
    The shared name proof can see a real identifier and its proton header before
    normalization absorbs them into a formula. Identifier-less headings only
    fence source; they do not gain a name or identifier.
    """
    proof_parts = list(parts)
    # A formula split after its declaration is analytical source, even when its
    # complete notation is unsupported. It cannot become a wrapped name prefix.
    for index, part in enumerate(parts):
        if not index or len(part) > 16_384 or analytical_excerpt(part):
            continue
        previous = proof_parts[index - 1]
        if len(previous) > 16_384:
            continue
        source_context = previous + ' ' + part
        if len(source_context.encode('utf-8')) > 16_384:
            continue
        label = _source_property_label(part)
        combined = previous + ' ' + label
        for finder in (find_ea_formula_candidate, find_hrms_formula_candidate):
            candidate = finder(combined)
            owned_end = (candidate.charge_end or candidate.end) if candidate is not None else 0
            property_tail = (candidate is not None and candidate.start > len(previous)
                             and _SOURCE_PROPERTY.match(combined[owned_end:].lstrip(' .:;')))
            if (candidate is not None and candidate.header_end <= len(previous) + 1
                    and (owned_end == len(combined.rstrip()) or property_tail)
                    and (terminal_identifier(label) is None
                         or candidate.start > len(previous) and re.match(r'C\d|\[[^\]]*C', label))):
                proof_parts[index] = source_context
                break
    starts = compound_name_starts(proof_parts)
    for index, part in enumerate(proof_parts):
        if len(part.encode('utf-8')) > 16_384:
            continue
        label = _source_property_label(part)
        cursor = index + 1
        property_bytes = 0
        while cursor < min(index + 4, len(parts)) and _SOURCE_PROPERTY.match(parts[cursor]):
            if analytical_excerpt(parts[cursor]):
                break
            property_bytes += len(parts[cursor].encode('utf-8'))
            if property_bytes > 16_384:
                break
            cursor += 1
        following = ' '.join(part[:16_384] for part in parts[cursor:cursor + 4]).lstrip()
        if not SUPPORTED_PROTON_HEADER.match(following):
            continue
        if terminal_identifier(label):
            saved = proof_parts[index:cursor]
            proof_parts[index:cursor] = [label] + [''] * (cursor - index - 1)
            start = compound_name_start(proof_parts, index)
            proof_parts[index:cursor] = saved
            if start is not None:
                starts.add(start)
        elif (index and analytical_excerpt(proof_parts[index - 1])
              and len(part.encode('utf-8')) <= 16_384
              and re.fullmatch(r"[A-Za-z(][A-Za-z0-9α-ωΑ-Ω,'’()\[\] −–—-]*", part)
              and re.search(r'[a-z]{3}', part) and re.search(r'[-−–—]', part)
              and not _SOURCE_PROPERTY.match(part) and analytical_excerpt(part) is None
              and balanced_name(part) and not unproven_bracket_fragment(part)):
            # An identifier-less physical heading is a fence only. Keep its
            # resolver outcome conservative; do not infer an identity or suffix.
            starts.add(index)
    return starts


def _normalize_body_text(text: str) -> str:
    """Apply shared whitespace and punctuation normalization before recognition."""
    text = text.replace("\n", " ")  # Removing all line breaks
    text = text.replace("\u00A0", " ")  # Non-breaking space
    text = text.replace("\u200B", "")   # Zero-width space (invisible space)
    text = text.replace("\u202F", " ")  # Narrow non-breaking space (used in numbers)
    text = text.replace("\u205F", " ")  # Middle mathematical space
    text = text.replace("\t", " ")      # Tab to space
    text = re.sub(r'\s{2,}', ' ', text)  # Replacing two or more spaces with one
    text = re.sub(r'(\d+)\s*(35|37|79|81|15|18)(Cl|Br|O|N)', r'\1\2\3', text) #Removing the extra space when recognizing isotopes
    text = re.sub(r'\s*:', ':', text) # Removing the space before the colon
    text = re.sub(r':(\S)', r': \1', text)  # Adding a space after the colon if it's missing 
    return text


def _paragraphs_from_text(text: str, normalized_path: str) -> list[str]:
    """Apply the existing paragraph normalization to extracted body text."""
    if text is None or not str(text).strip():
        raise EmptyTextError(f"No extractable text found in file: '{normalized_path}'")

    text = _normalize_body_text(text)
    
    #added a period after found 11.1111
    found_dot_pattern = r'(found[\s,:]*\d+(?:\.\d+)?)(?!\.)\b'
    text = re.sub(found_dot_pattern, r'\1.', text, flags=re.IGNORECASE)

    #Highlighting text blocks for check from the entire text. Adding a line break before "1H NMR".
    text = re.sub(
        r'(\s*)(?='
        r'1H[\s-]?NMR\s*\([^)]*(?:\([^)]*\)[^)]*)*,\s*\d+(?:\.\d+)?\s*MHz\)(: δ| δ)'  # Different variations of writing frequency and solvent in parentheses
        r'|'
        r'1H[\s-]?NMR\s*\(\d+(?:\.\d+)?\s*MHz,\s*[^)]*(?:\([^)]*\)[^)]*)*\)(: δ| δ)'
        r')',
        r'\n',
        text
    )

    #Dividing into paragraphs
    paragraphs = text.split("\n")

    from checksi.utils.text_processing import balance_brackets_in_text
    #Removing unmatched parentheses
    for i, paragraph in enumerate(paragraphs):
        paragraphs[i] = balance_brackets_in_text(paragraph)  # Rewriting the paragraph

    #Adding a line break at the end of the text block for check. 
    # Descriptions usually end with HRMS or EA data, where the word "found" appears. 
    # If HRMS or EA is not found, then add a line break after the last NMR.
    
    found_pattern = re.compile(
        r'(Found[\s,:-–—-]*\d[^\)]*?)'     # "Found", followed only by spaces or symbols, then a digit
        r'(\.\s)(?![^()]*\))',          # Up to the first ". " outside the parentheses
        re.DOTALL | re.IGNORECASE
    )
    # Two pattern variants with different positions of frequency and solvent
    nmr_pattern = re.compile(
        r'(?:'  
            r'NMR\s*\([^)]*(?:\([^)]*\)[^)]*)*,\s*\d+(?:\.\d+)?\s*MHz\)(: δ| δ)'
            r'|'  # или
            r'NMR\s*\(\d+(?:\.\d+)?\s*MHz,\s*[^)]*(?:\([^)]*\)[^)]*)*\)(: δ| δ)'
        r')'
        r'(.*?)'  
        r'(\.(?:\s|$))(?![^()]*\))',  # Up to the first ". " outside the parentheses
        re.DOTALL
    )
    
    # Finding the end - Processing each paragraph.
    for i, para in enumerate(paragraphs):
        para = found_pattern.sub(r'\1\2\n', para) #Adding a line break after HRMS or EA
        if not re.search(r'found', para, re.IGNORECASE):  # If HRMS or EA is not found, then add a line break after the last NMR.
            matches = list(re.finditer(nmr_pattern, para))
            if matches:
                last_match = matches[-1]
                # A truncated mass/EA section still belongs to this description.
                # Splitting at the NMR ending would discard its formula or let
                # a wrapped charge become part of the following compound name.
                tail = para[last_match.end():]
                following = next((part.lstrip() for part in paragraphs[i + 1:i + 4]
                                  if part.strip()), '')
                trailing_heading = (
                    terminal_identifier(tail) is not None
                    and SUPPORTED_PROTON_HEADER.match(following) is not None
                )
                # The continuation joiner already refuses a terminal heading
                # before the next spectrum. Keep that ambiguous flattened tail
                # separate here too, rather than assigning it to this compound.
                if analytical_excerpt(tail) is None or trailing_heading:
                    para = para[:last_match.end()] + '\n' + para[last_match.end():]
        paragraphs[i] = para  # Updating the paragraph
    
    #Splitting into paragraphs with new breaks
    result_text = "\n".join(paragraphs)
    paragraphs = result_text.split("\n")

    #Removing empty paragraphs
    paragraphs = [p for p in paragraphs if p.strip()]
    return _join_formula_continuations(paragraphs)


def _join_formula_continuations(paragraphs: list[str]) -> list[str]:
    """Keep complementary EA/HRMS sections with their preceding NMR description."""
    joined = []
    for index, paragraph in enumerate(paragraphs):
        marker = ANALYTICAL_MARKER.search(paragraph)
        qtof = _QTOF_HEADER.search(paragraph)
        first_marker = qtof if qtof and (marker is None or qtof.start() < marker.start()) else marker
        candidate = None
        if first_marker is qtof and qtof is not None:
            candidate = find_hrms_formula_candidate(paragraph)
        elif marker is not None and marker.group('ea'):
            candidate = find_ea_formula_candidate(paragraph)
        elif marker is not None and marker.group('ms'):
            candidate = find_hrms_formula_candidate(paragraph)
        # Candidate field ownership may start at the last alias in a header
        # (including TOF inside QTOF). Qualify the leading source prefix against
        # the first analytical marker instead of a partial instrument token.
        prefix = paragraph[:first_marker.start()] if candidate is not None else ''
        instrument_prefix = re.fullmatch(
            r'(?i)(?:(?:EI|CI|HR|HRMS|MS|GC|LC|UPLC|UHPLC|ESI|APCI|APPI|FAB|MALDI|QTOF|Q-TOF|TOF)\b|[\s.;:,()/+−–—-])*', prefix)
        property_prefix = len(prefix) <= 2048 and re.match(r'(?i)^\s*(?:IR|FT-?IR|m\.?\s*p\.?|Rf)\b', prefix)
        continuation = bool(candidate is not None and (instrument_prefix or property_prefix)
                            and terminal_identifier(prefix) is None)
        if (terminal_identifier(paragraph) is not None and index + 1 < len(paragraphs)
                and SUPPORTED_PROTON_HEADER.match(paragraphs[index + 1].lstrip())):
            # A flattened trailing name is not evidence that this whole segment
            # belongs to the previous compound. Preserve it for explicit review.
            continuation = False
        incoming_kinds = {item.lastgroup for item in ANALYTICAL_MARKER.finditer(paragraph)}
        if qtof is not None:
            incoming_kinds.add('ms')
        if (joined and continuation and SUPPORTED_PROTON_HEADER.search(joined[-1])
                and incoming_kinds <= {'ea', 'ms'}):
            previous_kinds = {item.lastgroup for item in ANALYTICAL_MARKER.finditer(joined[-1])}
            if _QTOF_HEADER.search(joined[-1]):
                previous_kinds.add('ms')
            # Repeated declarations remain separate and visible to coverage;
            # a name or another NMR section can never be a continuation.
            if incoming_kinds.isdisjoint(previous_kinds):
                joined[-1] = joined[-1].rstrip() + ' ' + paragraph.lstrip()
                continue
        joined.append(paragraph)
    return joined


def _is_in_header_or_footer_zone(
    block_bbox,
    page_height,
    *,
    header_margin: float,
    footer_margin: float,
) -> bool:
    """
    Checks if a text block is likely a header or footer based on its vertical position.

    Args:
        block_bbox (tuple): The bounding box (x0, y0, x1, y1) of the text block.
        page_height (float): The total height of the page.
        header_margin (float): The top margin as a fraction of page height.
        footer_margin (float): The bottom margin as a fraction of page height.

    Returns:
        bool: True if the block is in the header or footer zone, False otherwise.
    """
    _x0, y0, _x1, y1 = block_bbox
    header_zone_end = page_height * header_margin
    footer_zone_start = page_height * (1 - footer_margin)

    # A block is a header if its bottom edge is within the top margin.
    if y1 < header_zone_end:
        return True
        
    # A block is a footer if its top edge is within the bottom margin.
    if y0 > footer_zone_start:
        return True

    return False


def extract_text_from_pdf(
    pdf_path: str,
    *,
    header_margin: float,
    footer_margin: float,
) -> str:
    """
    Extracts text from a PDF, excluding headers and footers based on their geometry.
    """
    extracted_text = ""
    try:
        with fitz.open(pdf_path) as pdf:
            for page in pdf:
                page_height = page.rect.height
                if not page_height > 0:
                    continue

                # Extract text blocks with their coordinates, sorted by ??reading?? order.
                # Same flags as get_text("blocks") so block text is unchanged.
                textpage = page.get_textpage(flags=fitz.TEXTFLAGS_BLOCKS)
                blocks = page.get_text("blocks", sort=False, textpage=textpage)
                symbol_texts = _symbol_block_texts(page, textpage)
                for block in blocks:
                    # Block format: (x0, y0, x1, y1, text, block_no, block_type)
                    bbox = block[:4]
                    text_content = block[4]
                    original, mapped = symbol_texts.get(block[5], (None, None))
                    if original == text_content:
                        text_content = mapped

                    # Use the geometric filter to exclude headers and footers.
                    if not _is_in_header_or_footer_zone(
                        bbox,
                        page_height,
                        header_margin=header_margin,
                        footer_margin=footer_margin,
                    ):
                        # The text from get_text("blocks") already includes newlines (\n)
                        # for line breaks within the block itself.
                        extracted_text += text_content

                # Add a space between pages to ensure words from the end of one page
                # and the start of the next don't merge into a single word.
                extracted_text += " "
    except Exception as exc:
        raise FileReadError(f"Failed to extract text from PDF: '{pdf_path}'") from exc
            
    return extracted_text

def _read_docx_body(docx_path: str):
    """Load body paragraphs once, preserving native paragraph boundaries."""
    try:
        document = docx.Document(docx_path)
        source_paragraphs = ["".join(run.text for run in paragraph.runs) for paragraph in document.paragraphs]
        return document, source_paragraphs
    except Exception as exc:
        raise FileReadError(f"Failed to extract text from DOCX: '{docx_path}'") from exc


def extract_text_from_docx(docx_path):
    _, source_paragraphs = _read_docx_body(docx_path)
    return "\n".join(source_paragraphs)


DOCX_TABLE_CELL_LIMIT = 10_000
DOCX_SOURCE_REGION_LIMIT = 10_000
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"


def docx_paragraphs_and_omissions(docx_path: str) -> tuple[list[str], list[UnassessedDescription]]:
    """Load one package for both legacy body extraction and coverage inspection."""
    if not os.path.isfile(docx_path):
        raise InputFileNotFoundError(f"Input file not found: '{docx_path}'")
    document, source_paragraphs = _read_docx_body(docx_path)
    omissions = _inspect_docx_source(document)
    try:
        paragraphs = _paragraphs_from_docx_body(source_paragraphs, docx_path)
    except EmptyTextError:
        if not omissions:
            raise
        paragraphs = []
    return paragraphs, omissions


def _inspect_docx_source(document) -> list[UnassessedDescription]:
    """Inspect unsupported structures without failing usable body extraction."""
    omissions = []
    seen_cells = set()
    seen_tables = set()
    represented_text = set()
    cell_paths = {}

    def visit(table, table_number, ancestors):
        seen_tables.add(table._tbl)
        for row_number, row in enumerate(table.rows, start=1):
            column = row._tr.grid_before + 1
            # Physical cells avoid row.cells expanding an untrusted gridSpan,
            # and avoid aliases for horizontal/vertical merged cells.
            for cell_xml in row._tr.tc_lst:
                path = [*ancestors, {"table_1based": table_number,
                                    "row_1based": row_number, "column_1based": column}]
                column += cell_xml.grid_span
                if len(seen_cells) >= DOCX_TABLE_CELL_LIMIT:
                    omissions.append(unassessed_description("", {"kind": "docx_table", "table_path": path},
                                                           "docx_source_scan_limit"))
                    return False
                seen_cells.add(cell_xml)
                cell_paths[cell_xml] = path
                cell = _Cell(cell_xml, table)
                for paragraph_number, paragraph in enumerate(cell.paragraphs, start=1):
                    nodes = [node for node in paragraph._p.iter(qn("w:t"))
                             if not any(parent.tag == _MC_FALLBACK for parent in node.iterancestors())]
                    represented_text.update(nodes)
                    excerpt = analytical_excerpt(''.join(node.text or '' for node in nodes))
                    if excerpt is not None:
                        omissions.append(unassessed_description(excerpt, {
                            "kind": "docx_table", "table_path": path,
                            "paragraph_1based": paragraph_number,
                        }, "docx_table_not_supported"))
                for nested_number, nested in enumerate(cell.tables, start=1):
                    if not visit(nested, nested_number, path):
                        return False
        return True

    try:
        for paragraph in document.paragraphs:
            represented_text.update(paragraph._p.xpath('./w:r/w:t'))
        for table_number, table in enumerate(document.tables, start=1):
            if not visit(table, table_number, []):
                break
        # Retain tables skipped by public traversal (for example revisions).
        unvisited_tables = {table for table in document.element.body.iter(qn("w:tbl"))
                            if not any(parent.tag == _MC_FALLBACK for parent in table.iterancestors())} - seen_tables
        if unvisited_tables:
            omissions.append(unassessed_description("", {"kind": "document"},
                                                   "docx_table_structure_not_supported"))
        processed_regions = set()
        for node in document.element.body.iter(qn("w:t")):
            if node in represented_text or not (node.text or '').strip():
                continue
            ancestors = list(node.iterancestors())
            if any(parent.tag == _MC_FALLBACK for parent in ancestors):
                continue
            if any(ancestor in unvisited_tables for ancestor in ancestors):
                continue
            if any(parent.tag == qn("w:tc") and parent not in seen_cells for parent in ancestors):
                continue
            region = next((ancestor for ancestor in ancestors if ancestor.tag == qn("w:p")), node.getparent())
            if region in processed_regions:
                continue
            if len(processed_regions) >= DOCX_SOURCE_REGION_LIMIT:
                omissions.append(unassessed_description("", {"kind": "document"}, "docx_source_scan_limit"))
                break
            processed_regions.add(region)
            pieces, unextracted_spans, offset = [], [], 0
            for item in region.iter(qn("w:t")):
                if any(parent.tag == _MC_FALLBACK for parent in item.iterancestors()):
                    continue
                piece = item.text or ''
                pieces.append(piece)
                if item not in represented_text:
                    unextracted_spans.append((offset, offset + len(piece)))
                offset += len(piece)
            excerpt = analytical_excerpt(''.join(pieces), unextracted_spans=unextracted_spans)
            if excerpt is None:
                continue
            cell = next((ancestor for ancestor in ancestors if ancestor in cell_paths), None)
            location = {"kind": "docx_table", "table_path": cell_paths[cell]} if cell is not None else {"kind": "document"}
            omissions.append(unassessed_description(excerpt, location, "docx_content_not_supported"))
        deleted_regions = set()
        for node in document.element.body.iter(qn("w:delText")):
            if any(parent.tag == _MC_FALLBACK for parent in node.iterancestors()):
                continue
            region = next((parent for parent in node.iterancestors() if parent.tag == qn("w:del")), node)
            if region in deleted_regions:
                continue
            if len(deleted_regions) >= DOCX_SOURCE_REGION_LIMIT:
                omissions.append(unassessed_description("", {"kind": "document"}, "docx_source_scan_limit"))
                break
            deleted_regions.add(region)
            excerpt = analytical_excerpt(''.join(item.text or '' for item in region.iter(qn("w:delText"))))
            if excerpt is not None:
                omissions.append(unassessed_description(excerpt, {"kind": "document"},
                                                       "docx_deleted_content_not_supported"))
    except Exception:
        omissions.append(unassessed_description("", {"kind": "document"}, "docx_source_inspection_failed"))
    return omissions
