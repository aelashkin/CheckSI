"""Conservative source annotations tied to physical compound descriptions.

Normalization is only for locating text in a rendered source; it never changes
extraction or analysis. Unproven ownership remains explicitly unassociated.
"""
import re
from collections import defaultdict
from pathlib import Path

import fitz
from checksi.file_io import _is_in_header_or_footer_zone, symbol_span_text


COLORS = {'blue': (0.68, 0.85, 0.9), 'yellow': (1, 1, 0), 'red': (1, 0, 0)}


def _normalized(text: str) -> str:
    return ''.join(c.casefold().translate(str.maketrans('‐‑–—−', '-----'))
                   for c in text if not c.isspace() and c != '\u200b')


def _occurrences(text: str, phrase: str) -> list[tuple[int, int]]:
    if not phrase:
        return []
    found = []
    start = 0
    while (start := text.find(phrase, start)) >= 0:
        found.append((start, start + len(phrase)))
        start += len(phrase)
    return found


# Analysis appends "." after "found <number>" (file_io found_dot_pattern) so the
# record split sees the end of the description; the source PDF need not have it.
_FOUND_DOT = re.compile(r'(found[,:]*\d+(?:\.\d+)?)\.')


def _description_occurrences(text: str, phrase: str) -> list[tuple[int, int]]:
    """Exact occurrences; if none, retry with the inserted found-number periods optional."""
    hits = _occurrences(text, phrase)
    if hits or not _FOUND_DOT.search(phrase):
        return hits
    parts, last = [], 0
    for match in _FOUND_DOT.finditer(phrase):
        parts.append(re.escape(phrase[last:match.end(1)]) + r'\.?')
        last = match.end()
    parts.append(re.escape(phrase[last:]))
    return [match.span() for match in re.finditer(''.join(parts), text)]


def annotate_source(source: str, destination: Path, compounds: list[dict], summary: str, *,
                    header_margin: float = 0, footer_margin: float = 0) -> dict:
    """Annotate only phrases inside uniquely located, complete descriptions.

    Repeated identical descriptions are unassigned: extraction and conversion
    may reorder them, so occurrence counts alone cannot establish ownership.
    One note per compound carries its findings; rectangles carry no copied notes.
    """
    associations, ambiguities = [], []
    with fitz.open(source) as document:
        chars, locations = [], []
        for page in document:
            for block in page.get_text('rawdict', sort=False)['blocks']:
                if _is_in_header_or_footer_zone(block['bbox'], page.rect.height,
                        header_margin=header_margin, footer_margin=footer_margin):
                    continue
                for line in block.get('lines', []):
                    for span in line['spans']:
                        for char in span['chars']:
                            value = _normalized(symbol_span_text(char['c'], span.get('font', '')))
                            chars.extend(value)
                            locations.extend([(page.number, tuple(line['bbox']), tuple(char['bbox']))] * len(value))
        text = ''.join(chars)
        groups = defaultdict(list)
        for compound in compounds:
            groups[_normalized(compound.get('original_text', ''))].append(compound)
        contexts = {}
        for phrase, owners in groups.items():
            hits = _description_occurrences(text, phrase)
            if len(owners) == 1 and len(hits) == 1:
                contexts[owners[0]['id']] = hits[0]
            else:
                ambiguities.append({'compound_ids': [c['id'] for c in owners],
                                    'reason': 'Complete source description could not be uniquely associated',
                                    'physical_occurrences': len(hits)})
        # A substring description is not enough to distinguish two overlapping
        # source paragraphs. Refuse both owners rather than guessing.
        overlapping = set()
        entries = list(contexts.items())
        for index, (owner, (start, end)) in enumerate(entries):
            for other, (other_start, other_end) in entries[index + 1:]:
                if max(start, other_start) < min(end, other_end):
                    overlapping.update((owner, other))
        if overlapping:
            ambiguities.append({'compound_ids': sorted(overlapping),
                                'reason': 'Source description locations overlap'})
            for owner in overlapping:
                del contexts[owner]
        noted = set()
        for compound in compounds:
            context = contexts.get(compound['id'])
            if context is None:
                continue
            start, end = context
            info = compound.get('highlight_info', {})
            color = info.get('color') if info.get('color') in COLORS else 'blue'
            ranges = set()
            for field in ('name_text', 'hnmr_text', 'cnmr_text'):
                phrase = info.get(field)
                for a, b in _occurrences(text[start:end], _normalized(phrase or '')):
                    a += start
                    b += start
                    if (a, b) in ranges:
                        continue
                    ranges.add((a, b))
                    lines = defaultdict(list)
                    for page_num, line, rect in locations[a:b]:
                        lines[(page_num, line)].append(rect)
                    for (page_num, _), rects in lines.items():
                        rect = fitz.Rect(rects[0])
                        for other in rects[1:]:
                            rect |= fitz.Rect(other)
                        page = document[page_num]
                        annotation = page.add_highlight_annot(rect)
                        annotation.set_colors(stroke=COLORS[color])
                        note = ''
                        if compound['id'] not in noted:
                            note = '\n'.join([f"Compound {compound['id']}.", *compound.get('all_warnings', [])])
                            noted.add(compound['id'])
                        annotation.set_info(title=f"Compound {compound['id']}", content=note)
                        annotation.update()
                        associations.append({'compound_id': compound['id'], 'page_1based': page_num + 1,
                                             'phrase': phrase, 'rect': list(rect), 'color': color})
        if document.page_count:
            note = document[0].add_text_annot((18, 18), summary)
            note.set_info(title='stat', content=summary)
            note.update()
        document.save(destination, garbage=3, deflate=True)
    matched = {item['compound_id'] for item in associations}
    missing = [c['id'] for c in compounds if c['id'] not in matched]
    warnings = []
    if missing:
        warnings.append(f"Source annotations associated {len(matched)} of {len(compounds)} compounds; "
                        f"unmatched IDs: {', '.join(map(str, missing))}. Full source descriptions and findings remain in HTML/JSON.")
    return {'source_associations': associations, 'annotation_ambiguities': ambiguities,
            'annotation_warnings': warnings, 'highlighted_compounds': len(matched)}
