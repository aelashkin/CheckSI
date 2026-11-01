"""Application-level orchestration for one analysis run."""

from __future__ import annotations

import os
import threading
import tempfile
from pathlib import Path
from dataclasses import dataclass

from checksi.artifacts import reserve_run_directory
from checksi.assessment import build_assessment, run_completion_message
from checksi.config_schema import AppConfig, ErrorSeverity
from checksi.controller import CheckSIController
from checksi.dp5_enrichment import enrich_analysis_with_dp5, prepare_dp5_requests
from checksi.dp5q.client import DP5qAvailabilityError, check_availability
from checksi.dp5q.contracts import CheckRequest
from checksi.document_parser import DocumentParser
from checksi.errors.defs import AnalysisErrorCode
from checksi.errors.manager import get_error_severity
from checksi.errors.findings import issue_counts as finding_issue_counts
from checksi.errors.run_diagnostics import RunDiagnostics
from checksi.events import EventDispatcher, LogEvent, ProgressEvent, create_event_dispatcher, infer_log_severity
from checksi.report_service import empty_report_data, export_reports
from checksi.utils.platform_helpers import ConversionCancelled, convert_docx_to_pdf, open_system_file


@dataclass(slots=True)
class ApplicationRunResult:
    """Structured result from a single application run."""

    input_path: str
    analysis_data: dict
    report_data: dict
    issue_counts: dict[str, int]


class ApplicationRunner:
    """Coordinator that mediates parser -> analysis controller -> report service."""

    def __init__(
        self,
        *,
        document_parser: DocumentParser | None = None,
        event_dispatcher: EventDispatcher | None = None,
        log_callback=None,
        progress_callback=None,
    ) -> None:
        self._dispatch = create_event_dispatcher(
            event_dispatcher,
            log_callback=log_callback,
            progress_callback=progress_callback,
        )
        self._document_parser = document_parser or DocumentParser(event_dispatcher=self._dispatch)

    def _legacy_log_callback(self, message: str) -> None:
        self._dispatch(
            LogEvent(
                severity=infer_log_severity(message),
                message=message,
            )
        )

    def execute(
        self,
        cfg: AppConfig,
        input_path: str,
        cancel_token: threading.Event | None = None,
    ) -> ApplicationRunResult:
        """Execute one isolated settings snapshot, cancelling at safe boundaries."""
        snapshot = cfg.model_copy(deep=True)
        normalized_path = os.path.abspath(os.path.normpath(str(input_path)))
        snapshot.paths.input_path = Path(normalized_path)
        try:
            return self._execute_snapshot(snapshot, normalized_path, cancel_token)
        except Exception:
            if snapshot.paths.output_dir != cfg.paths.output_dir:
                try:
                    snapshot.paths.output_dir.rmdir()
                except OSError:
                    self._legacy_log_callback(f"Available artifacts retained under: {snapshot.paths.output_dir}")
            self._dispatch(ProgressEvent(0, 0, "Run failed"))
            raise

    def _execute_snapshot(
        self,
        cfg: AppConfig,
        input_path: str,
        cancel_token: threading.Event | None,
    ) -> ApplicationRunResult:
        diagnostics = RunDiagnostics(cfg)
        analysis_data = {
            "compounds": [],
            "name_finding_stats": {},
            "total_found": 0,
            "error_stats": {},
            "aborted": False,
            "processed_paragraphs": 0,
            "total_paragraphs": 0,
            "assessment": build_assessment(0, scan_complete=False),
        }
        pdf_path = input_path if input_path.lower().endswith(".pdf") else None
        report_data = empty_report_data()
        run_dir = None

        def reserve_destination() -> Path:
            nonlocal run_dir
            if run_dir is None:
                run_dir = reserve_run_directory(cfg.paths.output_dir, input_path)
                cfg.paths.output_dir = run_dir
                report_data['output_dir'] = str(run_dir)
            return run_dir

        def is_cancelled() -> bool:
            return bool(analysis_data.get("aborted")) or bool(
                cancel_token and cancel_token.is_set()
            )

        def start_phase(message: str) -> bool:
            if is_cancelled():
                return False
            self._dispatch(ProgressEvent(0, 0, message))
            return not is_cancelled()

        runtime_info = None
        if start_phase("Preflight"):
            reserve_destination()

        parsed = None
        if start_phase("Document extraction"):
            parsed = self._document_parser.parse(
                input_path,
                header_margin=cfg.modes.header_margin,
                footer_margin=cfg.modes.footer_margin,
            )
            input_path = parsed.input_path
            pdf_path = parsed.pdf_path
            analysis_data["total_paragraphs"] = len(parsed.paragraphs)
            analysis_data["assessment"] = build_assessment(
                0, parsed.source_omissions, scan_complete=False)

        if parsed is not None and start_phase("Analysis"):
            controller = CheckSIController(cfg, event_dispatcher=self._dispatch)
            analysis_data = controller.run_analysis(
                parsed.paragraphs,
                cancel_token=cancel_token,
                source_omissions=parsed.source_omissions,
                _diagnostics=diagnostics,
            )

        if cfg.dp5.enabled and start_phase("DP5 enrichment"):
            if cfg.dp5.keep_artifacts:
                reserve_destination()
            prepared_requests = prepare_dp5_requests(cfg, analysis_data, cancel_token=cancel_token)
            availability_error = None
            if not is_cancelled() and any(isinstance(item, CheckRequest) for item in prepared_requests):
                try:
                    runtime_info = check_availability(cfg.dp5)
                except DP5qAvailabilityError as exc:
                    availability_error = exc
                    runtime_info = {'available': False, 'reason': str(exc)}
            if not is_cancelled():
                enrich_analysis_with_dp5(
                    cfg,
                    analysis_data,
                    event_dispatcher=self._dispatch,
                    cancel_token=cancel_token,
                    runtime_info=runtime_info,
                    prepared_requests=prepared_requests,
                    _diagnostics=diagnostics if diagnostics.analysis_recorded else None,
                )
            if availability_error is not None and not is_cancelled():
                raise availability_error

        exported = False
        if start_phase("Report export"):
            destination = reserve_destination()
            warnings = []
            with tempfile.TemporaryDirectory(prefix="checksi-") as temporary:
                if Path(input_path).suffix.lower() == '.docx':
                    self._dispatch(ProgressEvent(0, 0, 'DOCX source conversion'))
                    def conversion_log(message):
                        warnings.append(message)
                        self._legacy_log_callback(message)
                    try:
                        pdf_path = convert_docx_to_pdf(
                            input_path, conversion_log, work_dir=temporary,
                            executable=cfg.conversion.libreoffice_path,
                            timeout_seconds=cfg.conversion.timeout_seconds,
                            cancel_token=cancel_token)
                    except ConversionCancelled:
                        analysis_data['aborted'] = True
                        pdf_path = None
                        warnings = ['DOCX source annotation skipped: conversion cancelled.']
                    if pdf_path:
                        warnings.clear()
                    elif not is_cancelled():
                        warnings = ['DOCX source annotation unavailable. ' + ' '.join(warnings)]
                # Analysis is complete at this boundary. Retain findings even
                # when optional source conversion was cancelled.
                report_data = export_reports(
                    cfg, analysis_data, pdf_path=str(pdf_path) if pdf_path else None,
                    log_callback=self._legacy_log_callback, run_dir=destination,
                    artifact_warnings=warnings, cancel_token=cancel_token,
                    _diagnostics=diagnostics.snapshot() if diagnostics.analysis_recorded else None)
                exported = True

        # Export is an indivisible writer operation. Keep its actual metadata if
        # cancellation arrived while it was writing, and never open those files.
        if is_cancelled():
            self._dispatch(
                LogEvent(
                    severity=ErrorSeverity.WARNING,
                    message=(
                        "Run cancelled during report export. Generated artifacts retained; automatic opening skipped."
                        if exported else
                        "Analysis aborted before completion. Skipping report export."
                    ),
                )
            )
        elif cfg.modes.open_reports_on_success:
            for path_key in ("pdf_path", "html_path", "error_stats_path", "source_pdf_path"):
                if is_cancelled():
                    break
                output_path = report_data.get(path_key)
                if output_path:
                    open_system_file(output_path, log=self._legacy_log_callback)

        run_aborted = is_cancelled()
        analysis_data["aborted"] = run_aborted
        report_data["aborted"] = run_aborted
        issue_counts = (finding_issue_counts(diagnostics.tracker.occurrences) if diagnostics.analysis_recorded
                        else _summarize_issue_counts(cfg, analysis_data.get("error_stats", {})))
        if run_dir is not None and not exported:
            # Empty reservations are owned resources; retained DP5q artifacts stay visible.
            try:
                run_dir.rmdir()
                report_data['output_dir'] = None
            except OSError:
                pass
        terminal_message = run_completion_message(analysis_data, report_data)
        self._dispatch(ProgressEvent(0, 0, terminal_message))
        return ApplicationRunResult(
            input_path=input_path,
            analysis_data=analysis_data,
            report_data=report_data,
            issue_counts=issue_counts,
        )


def _summarize_issue_counts(cfg: AppConfig, error_stats: dict[str, int]) -> dict[str, int]:
    by_severity = {
        ErrorSeverity.INFO: 0,
        ErrorSeverity.WARNING: 0,
        ErrorSeverity.CRITICAL: 0,
    }

    for error_name, count in error_stats.items():
        if int(count) <= 0:
            continue
        try:
            code = AnalysisErrorCode(error_name)
        except ValueError:
            continue

        severity = get_error_severity(cfg, code)
        if severity in by_severity:
            by_severity[severity] += int(count)

    warning_count = by_severity[ErrorSeverity.WARNING]
    critical_count = by_severity[ErrorSeverity.CRITICAL]
    return {
        "info": by_severity[ErrorSeverity.INFO],
        "warning": warning_count,
        "critical": critical_count,
        "anomaly_count": warning_count + critical_count,
    }
