"""Input parsing service for analysis runs."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from checksi.config_schema import ErrorSeverity
from checksi.assessment import UnassessedDescription
from checksi.events import EventDispatcher, LogEvent, create_event_dispatcher
from checksi.file_io import docx_paragraphs_and_omissions, paragraphs_from_input


@dataclass(slots=True)
class ParsedDocument:
    """Normalized parse result for a single analysis input."""

    input_path: str
    paragraphs: list[str]
    pdf_path: str | None
    source_omissions: list[UnassessedDescription] = field(default_factory=list)


class DocumentParser:
    """File-I/O boundary for parsing input documents and optional PDF conversion."""

    def __init__(self, *, event_dispatcher: EventDispatcher | None = None, log_callback=None):
        self._dispatch = create_event_dispatcher(
            event_dispatcher,
            log_callback=log_callback,
        )

    def _dispatch_log(
        self,
        message: str,
        severity: ErrorSeverity = ErrorSeverity.INFO,
    ) -> None:
        self._dispatch(LogEvent(severity=severity, message=message))

    def parse(
        self,
        input_path: str,
        *,
        header_margin: float,
        footer_margin: float,
    ) -> ParsedDocument:
        normalized_path = os.path.abspath(os.path.normpath(str(input_path)))
        if normalized_path.lower().endswith(".docx"):
            paragraphs, source_omissions = docx_paragraphs_and_omissions(normalized_path)
        else:
            paragraphs = paragraphs_from_input(
                normalized_path,
                header_margin=header_margin,
                footer_margin=footer_margin,
            )
            source_omissions = []
        pdf_path = self._resolve_pdf_path(normalized_path)
        return ParsedDocument(
            input_path=normalized_path,
            paragraphs=paragraphs,
            pdf_path=pdf_path,
            source_omissions=source_omissions,
        )

    def _resolve_pdf_path(self, input_path: str) -> str | None:
        lower_path = input_path.lower()
        if lower_path.endswith(".pdf"):
            return input_path
        if lower_path.endswith(".docx"):
            return None

        self._dispatch_log(
            "\nWarning: Input file is not a PDF or DOCX. PDF highlighting will be skipped.",
            severity=ErrorSeverity.WARNING,
        )
        return None
