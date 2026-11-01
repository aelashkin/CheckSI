"""One escaped HTML findings document, rendered offline by browsers and MuPDF."""
from __future__ import annotations

import base64
from collections import Counter
from copy import deepcopy
from html import escape
from importlib.resources import files
from pathlib import Path
import re
import unicodedata

import fitz
from lxml import html as html_parser

from checksi.config_schema import AppConfig, ErrorSeverity, OutputMode
from checksi.errors.findings import Finding
from checksi.errors.manager import format_finding
from checksi.errors.run_diagnostics import DiagnosticSnapshot
from checksi.assessment import REASON_LABELS, source_label
from checksi.reporting import generate_molecule_image
from checksi.utils.dp5_format import format_fixed, format_score_and_threshold


CSS = '''
@font-face {font-family: Report; src: url(NotoSans-Regular.ttf);}
@font-face {font-family: Report; font-weight: bold; src: url(NotoSans-Bold.ttf);}
body {font-family: Report; font-size: 10pt; line-height: 1.45; color: #202d3a;}
h1 {font-size: 22pt; color: #163e52; margin-bottom: 8pt;}
h2 {font-size: 15pt; color: #163e52; margin-top: 20pt;}
h3 {font-size: 11pt; margin-top: 14pt;}
p {margin: 6pt 0; overflow-wrap: anywhere;}
.source {white-space: pre-wrap;}
.critical {color: #a82020;} .warning {color: #8a4b00;} .info {color: #184870;}
table {border-collapse: collapse; width: 100%; font-size: 8pt; table-layout: fixed;}
th, td {border: 0.5pt solid #aebdc5; padding: 4pt; overflow-wrap: anywhere;}
th {text-align: left;} tr {page-break-inside: avoid;}
.molecule {width: 150pt; height: 150pt;}
.small {font-size: 8pt; color: #51616c;}
@media screen {body {max-width: 960px; margin: 32px auto; padding: 0 24px;}}
'''


def font_resources() -> dict[str, bytes]:
    """Load immutable installed font resources without writing site-packages."""
    root = files('checksi').joinpath('resources', 'fonts')
    return {name: root.joinpath(name).read_bytes() for name in ('NotoSans-Regular.ttf', 'NotoSans-Bold.ttf')}


def _text(value) -> str:
    return escape(str(value if value is not None else ''), quote=True)


def _token_chunks(word: str) -> list[str]:
    chunks = []
    start = 0
    while start < len(word):
        end = min(start + 12, len(word))
        while end < len(word) and (
            unicodedata.category(word[end]).startswith('M')
            or word[end] == '\u200d' or word[end - 1] == '\u200d'
        ):
            end += 1
        chunks.append(word[start:end])
        start = end
    return chunks


def _flow_text(value, *, wrap_token: bool = False) -> str:
    """Add invisible line-break opportunities without truncating escaped text."""
    text = str(value if value is not None else '')
    if not wrap_token:
        return _text(text)
    pieces = []
    previous = 0
    for match in re.finditer(r'\S{24,}', text):
        pieces.append(_text(text[previous:match.start()]))
        pieces.append('<wbr>'.join(_text(chunk) for chunk in _token_chunks(match.group())))
        previous = match.end()
    pieces.append(_text(text[previous:]))
    return ''.join(pieces)


def _number(value, suffix='') -> str:
    return '' if value is None else _text(format_fixed(value) + suffix)


def _formula_html(compound: dict) -> str:
    """Show source evidence beside a formula finding; retain all evidence in JSON."""
    formula_codes = {
        'name_formula_mismatch', 'name_formula_mass_delta', 'formula_source_mismatch',
        'formula_source_review_required', 'hrms_ion_charge_missing',
        'hrms_formula_charge_missing', 'hrms_charge_inconsistent',
        'hrms_ion_balance_invalid', 'hrms_adduct_mismatch',
        'calcd_formula_mass_mismatch', 'calcd_formula_mass_delta',
        'isotope_format_invalid', 'halogen_isotope_unspecified',
        'ea_isotope_calcd_mismatch',
    }
    warnings = compound.get('all_warnings', compound.get('warnings', []))
    codes = {match.group(1) for warning in warnings
             if (match := re.match(r'^(?:[⚠️❗\s])*([a-z][a-z0-9_]+)\. ', warning))}
    if (not any(code in formula_codes or code.startswith(('formula_', 'ea_formula_')) for code in codes)
            and not any('Formula not checked by CheckSI:' in warning for warning in warnings)):
        return ''
    rows = []
    labels = {'ea': 'EA declaration', 'hrms': 'HRMS ion', 'hrms_neutral': 'HRMS neutral', 'name': 'Accepted structure'}
    parent_state = (compound.get('formula_sources', {}).get('hrms_neutral', {}).get('composition') or {}).get('state')
    if parent_state == 'ion':
        labels['hrms_neutral'] = 'HRMS parent ion'
    for source, evidence in compound.get('formula_sources', {}).items():
        candidate = evidence.get('candidate') or {}
        composition = evidence.get('composition') or {}
        raw = candidate.get('raw', '')
        before_expansion = evidence.get('normalization', {}).get('pre_expansion_candidate')
        original = evidence.get('normalization', {}).get('pre_normalization_candidate')
        if original and original['raw'] != raw:
            raw = f"{original['raw']} → {raw} (legacy normalization)"
        elif before_expansion:
            raw = f"{before_expansion['raw']} → {raw} (legacy expansion)"
        if candidate.get('charge_raw'):
            raw += f"; declared charge {candidate['charge_raw']}"
        if not candidate and not evidence.get('formula'):
            continue
        rows.append('<tr>' + ''.join(f'<td>{_flow_text(value, wrap_token=index in (1, 2))}</td>' for index, value in enumerate((
            labels.get(source, source), raw, evidence.get('formula'),
            f"{evidence.get('status', 'unavailable')} / {composition.get('state', 'unknown')}",
            composition.get('reason') or candidate.get('reason') or evidence.get('status'),
        ))) + '</tr>')
    if not rows:
        return ''
    available_sources = {source for source, evidence in compound.get('formula_sources', {}).items()
                         if evidence.get('candidate') or evidence.get('formula')}
    comparisons = []
    for pair in compound.get('formula_comparisons', []):
        if pair['left'] not in available_sources or pair['right'] not in available_sources:
            continue
        comparisons.append('<tr>' + ''.join(f'<td>{_flow_text(value)}</td>' for value in (
            f"{labels.get(pair['left'], pair['left'])} / {labels.get(pair['right'], pair['right'])}",
            pair['status'], pair['reason'],
        )) + '</tr>')
    comparison_table = ('<table><tr><th>Formula-source pair</th><th>Relationship</th><th>Reason</th></tr>'
                        + ''.join(comparisons) + '</table>') if comparisons else ''
    return ('<h3>Formula source evidence</h3><table><tr><th>Source</th><th>Complete token (normalized input)</th>'
            '<th>Parsed composition</th><th>Formula status / state</th><th>Basis / reason</th></tr>' + ''.join(rows) + '</table>'
            + comparison_table)


def _carbon_scope_html(compound: dict) -> str:
    """Keep structural and observed scope visible independently of DP5q support."""
    parts = []
    scope = compound.get('carbon_scope') or {}
    labels = (
        ('full_environment_count', 'Full carbon environments'),
        ('observation_count', 'Original observations'),
        ('represented_signal_count', 'Represented signals'),
        ('accepted_missing_direct_cb_count', 'Accepted missing direct C–B environments'),
    )
    counts = [f'{label}: {_text(scope[key])}' for key, label in labels if scope.get(key) is not None]
    if counts:
        parts.append('<h3>Carbon evidence scope</h3><p>' + '; '.join(counts) + '.</p>')
    if scope.get('configuration_dependent'):
        expected = ' or '.join(str(count) for count in scope.get('expected_counts', []))
        parts.append(f'<p>Configuration-dependent carbon environments: {_text(expected)}. '
                     'The full count above describes the selected supported isomer.</p>')
    if scope.get('selected_isomer_smiles'):
        parts.append(f'<p class="small">Selected preparation isomer: {_text(scope["selected_isomer_smiles"])}.</p>')
    missing = scope.get('accepted_missing_direct_cb_environments') or []
    if missing:
        groups = ', '.join(','.join(str(label) for label in group) for group in missing)
        parts.append(f'<p class="info">Accepted unobserved direct C–B positions: {_text(groups)}. '
                     'The full structural carbon count is retained.</p>')
    for key, label in (('partition_reason', 'Diagnostic partition'),
                       ('scoring_partition_reason', 'Scoring partition'),
                       ('preparation_reason', 'DP5q preparation')):
        if scope.get(key):
            parts.append(f'<p class="small">{label}: {_text(scope[key])}.</p>')
    stereo = compound.get('stereo_assessment') or {}
    if stereo:
        parts.append(f'<p>Structural meso assessment: {_text(stereo.get("status"))}; '
                     f'configuration: {_text(stereo.get("configuration_status"))}; '
                     f'complete enumeration: {_text(stereo.get("enumeration_complete"))}.</p>')
        if stereo.get('reason'):
            parts.append(f'<p class="small">Structural assessment reason: {_text(stereo["reason"])}.</p>')
    return ''.join(parts)


def _dp5_html(compound: dict, cfg: AppConfig) -> str:
    result = compound.get('dp5')
    if not result:
        return ''
    diagnostics = result.get('diagnostics') or {}
    if result.get('status') != 'success':
        message = diagnostics.get('message') or result.get('message')
        info = result.get('message_severity') == 'info'
        message_html = (f'<p class="info"><b>Info:</b> {_text(message)}</p>' if info else
                        f'<p class="warning">{_text(message)}</p>') if message else ''
        return (f"<p>DP5q: {_text(result.get('status'))}; {_text(result.get('reason', ''))}</p>"
                + message_html)
    score = float(result.get('score_percent', result.get('probability_percent')))
    text, threshold = format_score_and_threshold(score, cfg.dp5.score_threshold_percent)
    parts = [f'<h3>DP5q compatibility score: {_text(text)}%</h3>',
             f'<p class="small">Advisory structural compatibility assessment; threshold {_text(threshold)}%. '
             'This score does not establish structural identity.</p>']
    shared = bool(diagnostics.get('coincident_observations'))
    if diagnostics.get('scope_note'):
        parts.append(f'<p class="small">{_text(diagnostics["scope_note"])}</p>')
    if diagnostics.get('scored_environment_count') is not None:
        parts.append(f'<p class="small">Scored carbon environments: {_text(diagnostics["scored_environment_count"])}.</p>')
    if shared:
        parts.append('<p class="small">Each shared peak is one original observation. Its evidence weight '
                     'is divided among the separate carbon environments; their predictions remain separate. '
                     'Original peak numbers below are 1-based indices into the source observation list.</p>')
    if shared or score < cfg.dp5.score_threshold_percent or cfg.modes.output_mode == OutputMode.DETAILED:
        atoms = sorted(result.get('atoms', []), key=lambda a: (a.get('assigned_shift') is None, -float(a.get('assigned_shift') or 0)))
        if atoms:
            titles = ['Carbon', 'Assigned 13C shift', 'Calculated shift', 'Corrected error', 'DP5q compatibility score']
            if shared:
                titles.extend(('Original peak (1-based)', 'Evidence weight'))
            parts.append('<table class="dp5"><thead><tr>' + ''.join(f'<th>{title}</th>' for title in titles)
                         + '</tr></thead><tbody>')
            occurrences = Counter(atom.get('observation_index') for atom in atoms)
            for atom in atoms:
                cells = [_text(atom.get('label')), _number(atom.get('assigned_shift')),
                         _number(atom.get('calculated_shift')), _number(atom.get('error')),
                         _number(atom.get('score_percent', atom.get('atom_probability_percent')), '%')]
                if shared:
                    index = atom.get('observation_index')
                    peak = str(index + 1) if type(index) is int and index >= 0 else ''
                    if peak and occurrences[index] > 1:
                        peak += ' (shared)'
                    cells.extend((_text(peak), _number(atom.get('evidence_weight'))))
                parts.append('<tr>' + ''.join(f'<td>{cell}</td>' for cell in cells) + '</tr>')
            parts.append('</tbody></table>')
        else:
            parts.append('<p>DP5q carbon assignment table was not available.</p>')
    if result.get('artifact_warning'):
        parts.append(f'<p class="warning">DP5q artifact warning: {_text(result["artifact_warning"])}</p>')
    return ''.join(parts)


def _diagnostic_rows(cfg: AppConfig, compound: dict, snapshot: DiagnosticSnapshot | None):
    """Use typed metadata on the live path; legacy dictionaries retain their adapter."""
    if snapshot is not None:
        for item in snapshot.displays.get(compound['id'], ()):
            if isinstance(item, Finding):
                if item.severity is ErrorSeverity.DISABLED:
                    continue
                text = format_finding(item)
                severity = item.severity.value
                prefix = f'<b>{severity.capitalize()}:</b> '
            else:
                text, severity, prefix = item.text, 'diagnostic', ''
            label = text.removeprefix('⚠️').removeprefix('❗').strip()
            yield label, severity, prefix
        return
    from checksi.errors.defs import AnalysisErrorCode
    from checksi.errors.manager import get_error_severity
    for warning in compound.get('all_warnings', []):
        label = warning.removeprefix('⚠️').removeprefix('❗').strip()
        match = re.match(r'^([a-z][a-z0-9_]+)\. ', label)
        try:
            code = AnalysisErrorCode(match.group(1)) if match else None
        except ValueError:
            code = None
        severity = get_error_severity(cfg, code).value if code is not None else 'diagnostic'
        prefix = f'<b>{severity.capitalize()}:</b> ' if code is not None else ''
        yield label, severity, prefix


def render_html(cfg: AppConfig, analysis: dict, summary: str, metadata: dict, source: dict,
                *, _diagnostics: DiagnosticSnapshot | None = None) -> tuple[str, dict[str, bytes]]:
    """Return self-contained HTML and the same resources used for PDF rendering."""
    resources = font_resources()
    css = CSS
    for name, data in resources.items():
        css = css.replace(f'url({name})', f'url(data:font/ttf;base64,{base64.b64encode(data).decode("ascii")})')
    parts = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             '<title>CheckSI findings</title><link rel="icon" href="data:,">', f'<style>{css}</style></head><body>',
             '<h1>CheckSI findings</h1>', f'<p><b>{_text(summary)}</b></p>',
             f'<p>Source: {_text(source["name"])}</p>',
             f'<p class="small">Source SHA-256: {_text((source.get("sha256") or "unavailable"))}</p>',
             '<p>Findings refer to the source descriptions below. The separate source-annotated.pdf is an annotated copy of the input, when available.</p>']
    assessment = analysis.get('assessment')
    if assessment is not None:
        status = assessment['status']
        style = 'info' if status == 'complete' else 'warning'
        descriptions = 'description' if assessment['assessed_descriptions'] == 1 else 'descriptions'
        parts.append(f'<h2 class="{style}">Assessment coverage: {_text(status)}</h2>')
        parts.append(f"<p>{assessment['assessed_descriptions']} supported {descriptions} assessed. "
                     "Coverage concerns detected content in the supported extraction scope. "
                     "Chemical validity requires separate evaluation.</p>")
        parts.append('<p class="small">Body text is inspected. Images, headers, footers and other unsupported '
                     'document regions are outside this scope. Detected analytical DOCX table text is retained '
                     'as unassessed content; table inspection is bounded.</p>')
        for omission in assessment['unassessed']:
            reason = REASON_LABELS.get(omission['reason'], omission['reason'])
            parts.append(f'<p class="warning"><b>{_text(source_label(omission["source"]))}:</b> {_text(reason)}.</p>')
            if omission['excerpt']:
                parts.append(f'<p class="source">{_flow_text(omission["excerpt"])}</p>')
    if metadata.get('source_pdf_path'):
        parts.append(f'<p>Source annotations associated {metadata["highlighted_compounds"]} of {len(analysis["compounds"])} discovered compounds.</p>')
    if metadata['artifact_warnings']:
        parts.append('<h2>Partial export</h2>')
        parts.extend(f'<p class="warning">{_text(message)}</p>' for message in metadata['artifact_warnings'])
    for message in metadata.get('annotation_warnings', []):
        parts.append(f'<p class="small">{_text(message)}</p>')
    parts.append('<h2>Finding counts</h2><table><tr><th>Finding code</th><th>Severity</th><th>Count</th></tr>')
    from checksi.errors.defs import AnalysisErrorCode
    from checksi.errors.manager import get_error_severity
    for code, count in analysis['error_stats'].items():
        if count:
            severity = (_diagnostics.severities[code] if _diagnostics is not None
                        else get_error_severity(cfg, AnalysisErrorCode(code)).value)
            parts.append(f'<tr><td>{_text(code)}</td><td>{_text(severity)}</td><td>{int(count)}</td></tr>')
    parts.append('</table>')
    if any(_formula_html(compound) for compound in analysis['compounds']):
        parts.append('<p class="small">Formula source parsing and comparisons are individual scientific checks. '
                     'Description assessment coverage and export completeness are separate. Formula token positions '
                     'in results.json refer to the normalized description, not original document coordinates.</p>')
    for compound in analysis['compounds']:
        number = compound['id']
        title = f'Compound {number}'
        if compound.get('name'):
            title += f': {compound["name"]}'
        if compound.get('identifier'):
            title += f' ({compound["identifier"]})'
        parts.append('<div class="compound">')
        parts.append(f'<h2 id="compound-{_text(number)}">{_text(title)}</h2>')
        parts.append(f'<p class="small">Source compound ID {_text(number)}; extracted paragraph {_text(compound.get("source_paragraph_1based", "not recorded"))}.</p>')
        if compound.get('smiles'):
            try:
                data = generate_molecule_image(compound['smiles']).getvalue()
                resources[f'molecule-{number}.png'] = data
                parts.append(f'<p><img class="molecule" alt="Molecular structure for compound {_text(number)}" src="data:image/png;base64,{base64.b64encode(data).decode("ascii")}"></p>')
            except (RuntimeError, ValueError) as exc:
                parts.append(f'<p>Structure image unavailable: {_text(exc)}. SMILES: {_text(compound["smiles"])}</p>')
        parts.append('<h3>Source description</h3>')
        parts.append(f'<p class="source">{_flow_text(compound.get("original_text", ""))}</p>')
        parts.append(_formula_html(compound))
        parts.append(_carbon_scope_html(compound))
        for label, severity, prefix in _diagnostic_rows(cfg, compound, _diagnostics):
            parts.append(f'<p class="{severity}">{prefix}{_flow_text(label)}</p>')
        parts.append(_dp5_html(compound, cfg))
        parts.append('</div>')
    parts.append('<p class="small">DP5q outcomes, full source descriptions, settings and statistics are retained in results.json. Statistical and analytical meanings are unchanged by rendering.</p></body></html>')
    return ''.join(parts), resources


_PDF_LAYOUT_CHARACTERS = str.maketrans({'ﬀ': 'ff', 'ﬁ': 'fi', 'ﬂ': 'fl', 'ﬃ': 'ffi',
                                      'ﬄ': 'ffl', 'ﬅ': 'st', 'ﬆ': 'st', '\u00ad': '', '\u200b': ''})


def _pdf_text(value: str) -> str:
    """Ignore layout breaks and typographic ligatures, retaining chemical punctuation."""
    return re.sub(r'\s+', '', unicodedata.normalize('NFC', value).translate(_PDF_LAYOUT_CHARACTERS))


class _PDFContent:
    """Required text occurrences and images for one ordered HTML fragment."""

    def __init__(self, body, resources: dict[str, bytes]):
        self.text = [text for part in body.itertext() if (text := _pdf_text(part))]
        self.identifiers = {element.get('id').removeprefix('compound-')
                            for element in body.iter('h2')
                            if re.fullmatch(r'compound-\d+', element.get('id', ''))}
        self.images = Counter(fitz.Pixmap(resources[element.get('src')]).digest
                              for element in body.iter('img'))

    def missing(self, document) -> str | None:
        rendered = []
        headings = set()
        images = Counter()
        flags = fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_LIGATURES & ~fitz.TEXT_PRESERVE_IMAGES
        for page in document:
            for block in page.get_text('dict', clip=page.rect, flags=flags)['blocks']:
                for line in block.get('lines', []):
                    spans = line['spans']
                    value = ''.join(span['text'] for span in spans)
                    rendered.append(value)
                    # A source paragraph mentioning an ID cannot replace its h2.
                    if any(span['size'] >= 14.9 and span['flags'] & 16 for span in spans):
                        match = re.match(r'^\s*Compound\s+(\d+)(?=[:(\s]|$)', value)
                        if match:
                            headings.add(match.group(1))
            for image in page.get_image_info(hashes=True):
                if page.rect.contains(fitz.Rect(image['bbox'])):
                    images[image['digest']] += 1
        missing_headings = self.identifiers - headings
        if missing_headings:
            return 'compound headings: ' + ', '.join(sorted(missing_headings))
        value = _pdf_text(''.join(rendered))
        cursor = 0
        for required in self.text:
            found = value.find(required, cursor)
            if found < 0:
                # Stop at the first omission: repeated failed searches through a
                # large card would make verification quadratic.
                return 'visible report text'
            cursor = found + len(required)
        if self.images - images:
            return 'molecular structure images'
        return None if document.page_count else 'all report pages'


def _pdf_sections(body) -> list[list]:
    """Keep the preamble with the first card and the footer with the last."""
    sections = []
    prefix = []
    for element in body:
        if element.tag == 'div' and 'compound' in element.get('class', '').split():
            sections.append(prefix + [element])
            prefix = []
        elif sections:
            sections[-1].append(element)
        else:
            prefix.append(element)
    return sections or [prefix]


def _split_pdf_blocks(elements) -> tuple[list, list] | None:
    """Divide a failed card at blocks, or a failed table between complete rows."""
    blocks = []
    for element in elements:
        if element.tag == 'div' and 'compound' in element.get('class', '').split():
            blocks.extend(element)
        else:
            blocks.append(element)
    if len(blocks) > 1:
        middle = len(blocks) // 2
        return blocks[:middle], blocks[middle:]
    if not blocks or blocks[0].tag != 'table':
        return None
    table = blocks[0]
    rows = list(table.iter('tr'))
    headers = {index for index, row in enumerate(rows) if row.find('th') is not None}
    data = [index for index in range(len(rows)) if index not in headers]
    if len(data) < 2:
        return None
    halves = []
    for selected in (data[:len(data) // 2], data[len(data) // 2:]):
        clone = deepcopy(table)
        keep = headers | set(selected)
        for index, row in enumerate(list(clone.iter('tr'))):
            if index not in keep:
                row.getparent().remove(row)
        halves.append([clone])
    return halves[0], halves[1]


def render_pdf(html: str, resources: dict[str, bytes], destination: Path) -> None:
    """Render the same HTML into A4 pages with the bundled fonts and no network."""
    # MuPDF's archive resolves local resources; browsers use the inline copies.
    archive = fitz.Archive()
    # Story does not honor browser wbr breaks; use its invisible break character.
    portable = html.replace("<wbr>", "&#8203;")
    for name, data in resources.items():
        archive.add((data, name))
        mime = 'font/ttf' if name.endswith('.ttf') else 'image/png'
        portable = portable.replace(f'data:{mime};base64,{base64.b64encode(data).decode("ascii")}', name)
    tree = html_parser.document_fromstring(portable, parser=html_parser.HTMLParser(no_network=True))
    body = tree.find('body')
    expected = _PDFContent(body, resources)
    # Story's overflow-wrap support differs from browsers. Make very long source
    # words breakable in the PDF without changing the HTML or chemical tokens.
    for element in body.iter():
        for attribute in ('text', 'tail'):
            value = getattr(element, attribute)
            if value:
                setattr(element, attribute, re.sub(
                    r'\S{80,}', lambda match: '\u200b'.join(_token_chunks(match.group())), value))
    portable = html_parser.tostring(tree, encoding='unicode', method='html')
    head_element = tree.find('head')
    head = (html_parser.tostring(head_element, encoding='unicode', method='html')
            if head_element is not None else '')
    media = fitz.paper_rect('a4')
    max_pages = min(10000, max(100, len(portable) // 100 + 100))
    pages_used = 0

    def page_rectangle(number, filled):
        nonlocal pages_used
        pages_used += 1
        if pages_used > max_pages:
            raise RuntimeError('Findings layout exceeded its page limit; use findings.html and results.json.')
        return media, media + (38, 38, -38, -38), None

    def fragment(elements):
        markup = '<html>' + head + '<body>' + ''.join(
            html_parser.tostring(element, encoding='unicode', method='html') for element in elements) + '</body></html>'
        return markup, _PDFContent(html_parser.fromstring(markup).find('body'), resources)

    def append_complete(elements, output):
        layout, content = fragment(elements)
        document = fitz.Story(layout, archive=archive).write_with_links(page_rectangle)
        try:
            missing = content.missing(document)
            if missing is None:
                output.insert_pdf(document)
                return
        finally:
            document.close()
        divided = _split_pdf_blocks(elements)
        if divided is None:
            raise RuntimeError(f'Findings PDF omitted {missing}; use findings.html and results.json.')
        for portion in divided:
            append_complete(portion, output)

    document = fitz.Story(portable, archive=archive).write_with_links(page_rectangle)
    try:
        missing = expected.missing(document)
        if missing is not None:
            document.close()
            document = fitz.open()
            # Separate Story fragments avoid the vendor's table/page-break skip.
            # Check every fragment before copying, including all table cells.
            for section in _pdf_sections(body):
                append_complete(section, document)
            missing = expected.missing(document)
        if missing is not None:
            raise RuntimeError(f'Findings PDF omitted {missing}; use findings.html and results.json.')
        document.subset_fonts()
        document.save(destination, garbage=3, deflate=True)
    finally:
        document.close()
    with fitz.open(destination) as check:
        if not check.is_pdf or not check.page_count:
            raise RuntimeError('Findings PDF is empty or unreadable.')
        if expected.missing(check) is not None:
            raise RuntimeError('Saved findings PDF lost report content; use findings.html and results.json.')
