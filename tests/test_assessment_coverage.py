"""Real PDF/DOCX coverage outcomes, source evidence and legacy extraction adapters."""

from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from importlib.resources import files
from io import StringIO
from html import unescape
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import fitz
from docx import Document

from checksi.config import ConfigManager
from checksi.config_schema import AppConfig
from checksi.document_parser import DocumentParser, ParsedDocument
from checksi.file_io import (
    EmptyTextError,
    extract_text_from_docx,
    paragraphs_from_input,
)
from checksi.main import main_cli


PROTON = "1H NMR (400 MHz, CDCl3): δ 3.50 (s, 2H), 1.20 (s, 6H)."
CARBON = "13C NMR (100 MHz, CDCl3): δ 60.0, 20.0."
SUPPORTED = f"{PROTON} {CARBON}"
HORIZONTAL_TABLE = "13C NMR (100 MHz, CDCl3): δ 91.1, 81.2."
VERTICAL_TABLE = (
    "HRMS (ESI): calcd for C2H7O+ [M+H]+ 47.0491, found 47.0491."
)
NESTED_TABLE = "1H NMR (400MHz, CDCl3): δ 8.73 (s, 1H)."
PLAIN_PROSE = "General experimental methods. Samples were stored in a dry cabinet."
COMPACT_ONE = "1H NMR (400MHz, CDCl3): δ 8.73 (s, 1H)."
# Headers without δ are not split into their own paragraph but are not admitted.
NO_DELTA_ONE = "1H NMR (300 MHz, CDCl3) 8.73 (s, 1H)."
NO_DELTA_TWO = "1H NMR (600 MHz, CDCl3) 6.19 (s, 1H)."
# Unsupported proton headers sit INSIDE one supported paragraph, before its
# final supported carbon section. They must not be dismissed because another
# header in that paragraph was assessed.
MIXED_PARAGRAPH = f"{PROTON} {NO_DELTA_ONE} {NO_DELTA_TWO} {CARBON}"


def save_pdf(source: Path, text: str) -> None:
    """Create an actual Unicode PDF, failing if the complete fixture will not fit."""
    with fitz.open() as document:
        page = document.new_page()
        remaining = page.insert_textbox(
            fitz.Rect(60, 70, 540, 750),
            text,
            fontfile=str(files("checksi").joinpath(
                "resources/fonts/NotoSans-Regular.ttf")),
            fontname="Noto",
            fontsize=10,
        )
        if remaining < 0:
            raise AssertionError("The complete coverage fixture must fit on the PDF page")
        document.save(source)


def save_plain_document(source: Path, text: str) -> None:
    if source.suffix == ".pdf":
        save_pdf(source, text)
        return
    document = Document()
    document.add_paragraph(text)
    document.save(source)


def save_table_only_document(source: Path) -> None:
    document = Document()
    document.add_table(rows=1, cols=1).cell(0, 0).text = NESTED_TABLE
    document.save(source)


def save_nested_and_merged_document(source: Path) -> None:
    """One supported body and four physical analytical cells in the table tree."""
    document = Document()
    document.add_paragraph(SUPPORTED)
    table = document.add_table(rows=2, cols=3)
    table.cell(0, 0).merge(table.cell(0, 1)).text = HORIZONTAL_TABLE
    table.cell(0, 2).merge(table.cell(1, 2)).text = VERTICAL_TABLE
    # This is a different, unmerged physical cell with the same text. Text-based
    # deduplication would incorrectly lose its omission and location.
    table.cell(1, 1).text = HORIZONTAL_TABLE
    nested_cell = table.cell(1, 0)
    nested_cell.text = "Supporting dataset."
    inner_cell = nested_cell.add_table(rows=1, cols=1).cell(0, 0)
    inner_cell.text = "Supporting dataset."
    inner_cell.add_table(rows=1, cols=1).cell(0, 0).text = NESTED_TABLE
    document.save(source)


class RealDocumentCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory(
            prefix="checksi-coverage-")))

    def cli_run(self, source: Path, *, severity="disabled"):
        """Keep extraction/controller/reports real; replace only DOCX conversion."""
        cfg = AppConfig.model_validate({
            "paths": {"input_path": str(source), "output_dir": str(self.root / "out")},
            "modes": {
                "deep_name_analysis": "off", "output_mode": "essential",
                "open_reports_on_success": False,
            },
            "solvents": {"entries": []},
            "errors": {"default_severity": severity},
            "dp5": {"enabled": False},
        })
        settings = self.root / f"{source.stem}-settings.toml"
        ConfigManager(settings).save(cfg)
        stdout, stderr = StringIO(), StringIO()
        with (
            patch("checksi.application_runner.convert_docx_to_pdf", return_value=None),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            status = main_cli(["--config-file", str(settings), "run", str(source)])
        summary = stdout.getvalue()
        match = re.search(r"^results_json: (.+)$", summary, re.MULTILINE)
        payload = None
        if match and match.group(1) != "not generated":
            payload = json.loads(Path(match.group(1)).read_text(encoding="utf-8"))
        return status, summary, stderr.getvalue(), payload

    def assert_incomplete_outcome(self, source, *, assessed, status):
        exit_code, summary, stderr, payload = self.cli_run(source)
        self.assertEqual(exit_code, 3, summary + stderr)
        self.assertIn(f"compounds_detected: {assessed}", summary)
        self.assertIn("critical_issues: 0", summary)
        self.assertIn("warning_issues: 0", summary)
        self.assertIsNotNone(payload, summary + stderr)
        self.assertEqual(payload["analysis"]["total_found"], assessed)
        self.assertTrue(all(count == 0 for count in payload["analysis"]["error_stats"].values()))
        # DOCX conversion is deliberately unavailable, so its export is partial
        # independently of assessment. PDF artifact exports remain complete.
        expected_export = "partial" if source.suffix == ".docx" else "complete"
        self.assertIn(f"export_status: {expected_export}", summary)
        self.assertEqual(payload["export"]["status"], expected_export)
        self.assertIn("assessment", payload["analysis"],
                      "Assessment coverage is missing from the analysis")
        assessment = payload["analysis"]["assessment"]
        self.assertEqual(assessment["status"], status)
        self.assertEqual(assessment["assessed_descriptions"], assessed)
        self.assertIn(f"assessment_status: {status}", summary)
        html = Path(payload["export"]["html_path"]).read_text(encoding="utf-8")
        self.assertIn(f"Assessment coverage: {status}", html)
        with fitz.open(payload["export"]["pdf_path"]) as report:
            text = " ".join(" ".join(page.get_text() for page in report).split())
        self.assertIn(f"Assessment coverage: {status}", text)
        for omission in assessment["unassessed"]:
            self.assertTrue(str(omission["reason"]).strip())
            self.assertIsInstance(omission["excerpt"], str)
            self.assertIsInstance(omission["source"], dict)
            self.assertIn(f"unassessed_records: {len(assessment['unassessed'])}", summary)
            if omission["excerpt"]:
                self.assertIn(omission["excerpt"], unescape(html))
        return payload, assessment


class AssessmentExitTests(RealDocumentCase):
    def test_partial_assessment_keeps_findings_and_exit_three_precedence(self):
        source = self.root / "partial-with-findings.pdf"
        save_pdf(source, SUPPORTED + " " + COMPACT_ONE)
        status, summary, stderr, payload = self.cli_run(source, severity="critical")
        self.assertEqual(status, 3, summary + stderr)
        self.assertEqual(payload["analysis"]["assessment"]["status"], "partial")
        self.assertGreater(sum(payload["analysis"]["error_stats"].values()), 0)
        self.assertRegex(summary, r"critical_issues: [1-9]\d*")
        self.assertIn("export_status: complete", summary)


class BodyExtractionCompatibilityTests(RealDocumentCase):
    """Body and list adapters keep their contracts beside DOCX source inspection."""

    def test_body_adapters_do_not_promote_nested_or_merged_tables_to_supported_text(self):
        source = self.root / "body-and-tables.docx"
        save_nested_and_merged_document(source)
        self.assertEqual(extract_text_from_docx(str(source)), SUPPORTED)
        parsed = DocumentParser().parse(str(source), header_margin=0, footer_margin=0)
        self.assertEqual([text.strip() for text in parsed.paragraphs], [SUPPORTED])
        self.assertNotIn(NESTED_TABLE, " ".join(parsed.paragraphs))

    def test_table_only_body_list_adapter_keeps_its_existing_empty_text_error(self):
        source = self.root / "table-only.docx"
        save_table_only_document(source)
        self.assertEqual(Document(source).paragraphs, [])
        self.assertEqual(extract_text_from_docx(str(source)), "")
        with self.assertRaises(EmptyTextError):
            paragraphs_from_input(str(source), header_margin=0, footer_margin=0)

    def test_legacy_parsed_document_constructor_has_an_independent_default_sidecar(self):
        first = ParsedDocument("input.pdf", [], None)
        second = ParsedDocument("input.pdf", [], None)
        first.source_omissions.append({"source": {"kind": "document"}, "excerpt": "", "reason": "scan_incomplete"})
        self.assertEqual(second.source_omissions, [])

    def test_parser_retains_table_only_evidence_without_adding_body_paragraphs(self):
        source = self.root / "parser-table-only.docx"
        save_table_only_document(source)
        parsed = DocumentParser().parse(str(source), header_margin=0, footer_margin=0)
        self.assertEqual(parsed.paragraphs, [])
        omission, = parsed.source_omissions
        self.assertIn("400MHz", omission["excerpt"])

    def test_revision_marked_table_is_explicitly_unassessed_without_invented_coordinates(self):
        from docx.oxml import OxmlElement
        source = self.root / "revision-table.docx"
        document = Document()
        document.add_paragraph(SUPPORTED)
        table = document.add_table(rows=1, cols=1)
        table.cell(0, 0).text = NESTED_TABLE
        revision = OxmlElement("w:ins")
        table._tbl.addprevious(revision)
        revision.append(table._tbl)
        document.save(source)
        parsed = DocumentParser().parse(str(source), header_margin=0, footer_margin=0)
        omission, = parsed.source_omissions
        self.assertEqual(omission["reason"], "docx_table_structure_not_supported")
        self.assertEqual(omission["source"], {"kind": "document"})

    def test_table_inspection_limit_retains_an_explicit_omission(self):
        source = self.root / "limited-tables.docx"
        save_nested_and_merged_document(source)
        with patch("checksi.file_io.DOCX_TABLE_CELL_LIMIT", 1):
            parsed = DocumentParser().parse(str(source), header_margin=0, footer_margin=0)
        self.assertTrue(any(item["reason"] == "docx_source_scan_limit" for item in parsed.source_omissions))

    def test_large_merge_span_does_not_expand_the_table_grid(self):
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        source = self.root / "large-merge-span.docx"
        document = Document()
        table = document.add_table(rows=1, cols=2)
        first, second = table.rows[0].cells
        first.text = HORIZONTAL_TABLE
        second.text = VERTICAL_TABLE
        span = OxmlElement("w:gridSpan")
        span.set(qn("w:val"), "1000000000")
        first._tc.get_or_add_tcPr().append(span)
        document.save(source)
        parsed = DocumentParser().parse(str(source), header_margin=0, footer_margin=0)
        self.assertEqual(len(parsed.source_omissions), 2)
        self.assertEqual(parsed.source_omissions[1]["source"]["table_path"][0]["column_1based"], 1000000001)


class CoverageOutcomeTests(RealDocumentCase):
    """Retain supported compounds while making unassessed content explicit."""

    def test_unrecognized_pdf_cannot_exit_clean(self):
        source = self.root / "unsupported-header.pdf"
        save_pdf(source, SUPPORTED.replace("400 MHz", "400MHz"))
        self.assert_incomplete_outcome(source, assessed=0, status="none")

    def test_mixed_pdf_cannot_exit_clean(self):
        source = self.root / "mixed-headers.pdf"
        save_pdf(source, SUPPORTED + " " + SUPPORTED.replace("400 MHz", "400MHz"))
        self.assert_incomplete_outcome(source, assessed=1, status="partial")

    def test_complete_assessment_and_optional_export_failure_have_independent_outcomes(self):
        source = self.root / "supported.pdf"
        save_pdf(source, SUPPORTED)
        status, summary, stderr, payload = self.cli_run(source)
        self.assertEqual(status, 0, summary + stderr)
        self.assertEqual(payload["analysis"]["assessment"], {
            "status": "complete", "assessed_descriptions": 1, "unassessed": [],
        })
        self.assertEqual(payload["export"]["status"], "complete")
        html = Path(payload["export"]["html_path"]).read_text(encoding="utf-8")
        self.assertIn("1 supported description assessed", html)
        self.assertNotIn("1 descriptions assessed", html)
        with patch("checksi.report_service.render_pdf", side_effect=OSError("PDF unavailable")):
            status, summary, stderr, partial = self.cli_run(source)
        self.assertEqual(status, 3, summary + stderr)
        self.assertEqual(partial["analysis"]["assessment"], payload["analysis"]["assessment"])
        self.assertEqual(partial["export"]["status"], "partial")
        self.assertIn("assessment_status: complete", summary)
        self.assertIn("export_status: partial", summary)

    def test_table_only_with_empty_body_is_an_incomplete_assessment_not_a_system_error(self):
        source = self.root / "analytical-table-without-body.docx"
        save_table_only_document(source)
        self.assertEqual(Document(source).paragraphs, [])
        _, assessment = self.assert_incomplete_outcome(source, assessed=0, status="none")
        omissions = assessment["unassessed"]
        self.assertEqual(len(omissions), 1)
        self.assertIn("400MHz", omissions[0]["excerpt"])
        self.assertEqual(omissions[0]["source"]["kind"], "docx_table")
        self.assertEqual(omissions[0]["source"]["table_path"], [
            {"table_1based": 1, "row_1based": 1, "column_1based": 1},
        ])

    def test_nested_and_merged_cell_evidence_is_complete_without_duplicate_aliases(self):
        source = self.root / "nested-and-merged.docx"
        save_nested_and_merged_document(source)
        payload, assessment = self.assert_incomplete_outcome(source, assessed=1, status="partial")
        self.assertEqual(len(payload["analysis"]["compounds"]), 1)
        omissions = assessment["unassessed"]
        self.assertEqual(len(omissions), 4,
                         "Two merge aliases must collapse, but identical physical cells must remain")
        self.assertTrue(all(item["source"]["kind"] == "docx_table" for item in omissions))
        excerpt_counts = Counter(
            marker for item in omissions
            for marker in ("91.1, 81.2", "47.0491", "400MHz")
            if marker in item["excerpt"]
        )
        self.assertEqual(excerpt_counts, {
            "91.1, 81.2": 2, "47.0491": 1, "400MHz": 1,
        })
        paths = {
            tuple((step["table_1based"], step["row_1based"], step["column_1based"])
                  for step in item["source"]["table_path"])
            for item in omissions
        }
        self.assertEqual(paths, {
            ((1, 1, 1),),
            ((1, 1, 3),),
            ((1, 2, 2),),
            ((1, 2, 1), (1, 1, 1), (1, 1, 1)),
        })

    def test_two_unsupported_proton_blocks_inside_one_supported_paragraph_are_retained(self):
        for extension in ("pdf", "docx"):
            with self.subTest(format=extension):
                source = self.root / f"mixed-inside-one-paragraph.{extension}"
                save_plain_document(source, f"General experimental methods. {MIXED_PARAGRAPH}")
                parsed = DocumentParser().parse(str(source), header_margin=0, footer_margin=0)
                analytical = [p for p in parsed.paragraphs if "1H NMR" in p]
                self.assertEqual(len(analytical), 1, parsed.paragraphs)
                self.assertIn("300 MHz", analytical[0])
                self.assertIn("600 MHz", analytical[0])
                payload, assessment = self.assert_incomplete_outcome(
                    source, assessed=1, status="partial")
                omissions = assessment["unassessed"]
                self.assertEqual(len(omissions), 2)
                paragraph = payload["analysis"]["compounds"][0]["source_paragraph_1based"]
                self.assertTrue(all(item["source"]["kind"] == "paragraph" for item in omissions))
                self.assertTrue(all(item["source"]["paragraph_1based"] == paragraph
                                    for item in omissions))
                for frequency in ("300 MHz", "600 MHz"):
                    self.assertEqual(sum(frequency in item["excerpt"] for item in omissions), 1)

    def test_nonanalytical_prose_has_explicit_no_recognition_without_chemistry_findings(self):
        for extension in ("pdf", "docx"):
            with self.subTest(format=extension):
                source = self.root / f"plain-nonanalytical.{extension}"
                save_plain_document(source, PLAIN_PROSE)
                payload, assessment = self.assert_incomplete_outcome(
                    source, assessed=0, status="none")
                self.assertEqual(payload["analysis"]["compounds"], [])
                self.assertTrue(assessment["unassessed"],
                                "Zero recognition needs an inspectable document-level reason")
                self.assertTrue(any(item["source"]["kind"] == "document"
                                    for item in assessment["unassessed"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
