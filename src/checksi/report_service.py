"""Run-owned report export; rendering never mutates analysis results."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import threading
from typing import Callable

import fitz

from checksi.artifacts import reserve_run_directory
from checksi.config_schema import AppConfig
from checksi.errors.run_diagnostics import DiagnosticSnapshot
from checksi.report_rendering import render_html, render_pdf
from checksi.source_annotations import annotate_source
from checksi.utils.dp5_format import format_fixed, format_score_and_threshold, format_skip_reasons
from checksi.utils.error_handler import export_error_stats


def empty_report_data() -> dict:
    """Metadata never advertises a file before it exists."""
    return {"html_path": None, "pdf_path": None, "analysis_path": None,
            "source_pdf_path": None, "error_stats_path": None, "output_dir": None,
            "highlighted_compounds": 0, "source_associations": [],
            "partial": False, "artifact_warnings": [], "status": "incomplete",
            "annotation_warnings": [], "annotation_ambiguities": []}


def _source_metadata(path: Path) -> dict:
    digest = hashlib.sha256()
    try:
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
    except OSError:
        return {"name": path.name, "path": str(path.absolute()), "sha256": None}
    return {"name": path.name, "path": str(path.absolute()), "sha256": digest.hexdigest()}


def export_reports(
    cfg: AppConfig,
    analysis_data: dict,
    *,
    pdf_path: str | None = None,
    log_callback: Callable[[str], None] | None = None,
    run_dir: Path | None = None,
    artifact_warnings: list[str] | None = None,
    cancel_token: threading.Event | None = None,
    _diagnostics: DiagnosticSnapshot | None = None,
) -> dict:
    """Save required HTML/JSON plus optional PDF/statistics/source annotations.

    A caller-owned run directory is private and already reserved. Direct exports
    reserve their own directory atomically, including when exports compete.
    Optional failures retain findings and advertise partial status in both formats.
    """
    log = log_callback or print
    if "error_stats" not in analysis_data:
        raise RuntimeError("analysis_data is missing required 'error_stats'")
    destination = run_dir or reserve_run_directory(cfg.paths.output_dir, cfg.paths.input_path)
    metadata = empty_report_data()
    metadata['output_dir'] = str(destination)
    metadata['artifact_warnings'] = list(artifact_warnings or [])
    for compound in analysis_data['compounds']:
        if compound.get('dp5', {}).get('artifact_warning'):
            metadata['artifact_warnings'].append(compound['dp5']['artifact_warning'])
    source = _source_metadata(cfg.paths.input_path)
    colors = {color: sum(c.get('highlight_info', {}).get('color', 'blue') == color
                        for c in analysis_data['compounds']) for color in ('blue', 'yellow', 'red')}
    total = analysis_data['total_found']
    summary = f"{total} {'compound was' if total == 1 else 'compounds were'} found. " + ', '.join(f'{c}: {n}' for c, n in colors.items())
    dp5 = _format_dp5_summary(analysis_data.get('dp5_summary'), cfg.dp5.score_threshold_percent)
    if dp5:
        summary += '. ' + dp5

    try:
        package_version = importlib.metadata.version('checksi')
    except importlib.metadata.PackageNotFoundError:
        package_version = None

    def save_json() -> None:
        metadata['aborted'] = bool(analysis_data.get('aborted') or (cancel_token and cancel_token.is_set()))
        metadata['partial'] = bool(metadata['artifact_warnings'])
        payload = {"schema_version": 2, "source": source, "analysis": analysis_data,
                   "configuration": cfg.model_dump(mode='json'),
                   "finding_severities": {name: (_diagnostics.severities[name] if _diagnostics is not None
                                                  else _severity(cfg, name)) for name in analysis_data['error_stats']},
                   "source_associations": metadata['source_associations'],
                   "export": {key: value for key, value in metadata.items()
                              if key not in ("source_associations", "annotation_ambiguities", "annotation_warnings")},
                   "annotation_ambiguities": metadata["annotation_ambiguities"],
                   "annotation_warnings": metadata["annotation_warnings"],
                   "provenance": {"renderer": 'PyMuPDF Story', 'pymupdf': fitz.VersionBind,
                                  'font': 'Noto Sans Regular/Bold',
                                  'checksi': package_version}}
        nonfinite = []
        payload = _json_finite(payload, '', nonfinite)
        payload['nonfinite_numbers'] = nonfinite
        target = destination / 'results.json'
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        metadata['analysis_path'] = str(target)

    # Preserve machine-readable findings even if later formatting fails.
    save_json()
    html_target = destination / 'findings.html'

    def required_html() -> tuple[str, dict[str, bytes]]:
        try:
            diagnostics = {'_diagnostics': _diagnostics} if _diagnostics is not None else {}
            html, resources = render_html(cfg, analysis_data, summary, metadata, source, **diagnostics)
            html_target.write_text(html, encoding='utf-8')
            metadata['html_path'] = str(html_target)
            return html, resources
        except Exception:
            metadata['status'] = 'failed'
            save_json()
            raise

    required_html()

    def optional(label: str, key: str, filename: str, writer, *, cancellable=True) -> None:
        target = destination / filename
        if cancellable and cancel_token and cancel_token.is_set():
            metadata['artifact_warnings'].append(f'{label} skipped: run cancelled.')
            return
        try:
            writer(target)
            if not target.is_file() or not target.stat().st_size:
                raise RuntimeError('the exporter did not produce a nonempty file')
            metadata[key] = str(target)
        except Exception as exc:  # Optional exporters must never discard available findings.
            message = f'{label} unavailable: {exc}'
            try:
                target.unlink(missing_ok=True)
            except OSError as cleanup_error:
                message += f' Incomplete file could not be removed at {target}: {cleanup_error}'
            metadata['artifact_warnings'].append(message)
            log('Warning: ' + message)

    optional('Statistics XLSX', 'error_stats_path', 'statistics.xlsx',
             lambda target: export_error_stats(analysis_data['error_stats'], source['name'], str(target),
                 log=log, open_file=False, compounds=analysis_data['compounds']), cancellable=False)
    if pdf_path:
        def annotate(target):
            metadata.update(annotate_source(pdf_path, target, analysis_data['compounds'], summary,
                header_margin=cfg.modes.header_margin, footer_margin=cfg.modes.footer_margin))
        optional('Annotated source PDF', 'source_pdf_path', 'source-annotated.pdf', annotate)
        for message in metadata['annotation_warnings']:
            log('Source annotation coverage: ' + message)
    html, resources = required_html()
    optional('Findings PDF', 'pdf_path', 'findings.pdf', lambda target: render_pdf(html, resources, target))
    metadata['partial'] = bool(metadata['artifact_warnings'])
    metadata['status'] = ('cancelled' if analysis_data.get('aborted') or (cancel_token and cancel_token.is_set())
                          else 'partial' if metadata['partial'] else 'complete')
    # Include any optional-export failure in the required readable/semantic reports.
    required_html()
    save_json()
    log(f"Findings saved: {html_target}")
    return metadata


def _json_finite(value, path: str, nonfinite: list):
    """Copy JSON data, recording every nonfinite value without mutating analysis."""
    if isinstance(value, float) and not math.isfinite(value):
        nonfinite.append({'path': path, 'value': 'NaN' if math.isnan(value) else 'Infinity' if value > 0 else '-Infinity'})
        return None
    if isinstance(value, dict):
        return {key: _json_finite(item, f"{path}/{str(key).replace('~', '~0').replace('/', '~1')}", nonfinite) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_finite(item, f'{path}/{index}', nonfinite) for index, item in enumerate(value)]
    return value


def _severity(cfg: AppConfig, name: str) -> str:
    from checksi.errors.defs import AnalysisErrorCode
    from checksi.errors.manager import get_error_severity
    return get_error_severity(cfg, AnalysisErrorCode(name)).value


def _format_dp5_summary(summary: dict | None, threshold: float | None = None) -> str | None:
    if not summary or not summary.get("enabled"):
        return None

    reasons = format_skip_reasons(summary.get("skipped_reasons"))
    base = (
        f"DP5q compatibility score statistics (n={int(summary.get('n', 0))}, "
        f"skipped={int(summary.get('skipped', 0))}, "
        f"failed={int(summary.get('failed', 0))}"
        f"{f'; skip reasons: {reasons}' if reasons else ''})"
    )
    if int(summary.get("n", 0)) <= 0:
        return f"{base}."

    def statistic(name: str) -> str:
        value = float(summary.get(f"{name}_score_percent", summary.get(f"{name}_probability_percent")))
        # Match the per-compound text, so a flagged 9.998% minimum never reads as 10.00%.
        return format_score_and_threshold(value, threshold)[0] if threshold is not None else format_fixed(value)

    return f"{base}: min={statistic('min')}%, max={statistic('max')}%, mean={statistic('mean')}%."
