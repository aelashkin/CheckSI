"""Characterize supported extraction and description preprocessing behavior."""

from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from docx import Document

from checksi import file_io


def text_block(text: str, top: float, bottom: float) -> tuple:
    """Build the text-block shape returned by PyMuPDF."""
    return (0.0, top, 100.0, bottom, text, 0, 0)


def pdf_page(height: float, blocks: list[tuple]) -> SimpleNamespace:
    """Build a controlled PDF page without creating a document fixture."""
    def get_text(kind: str = "text", **_kwargs):
        # Span dictionaries are only read to find Symbol-font glyphs.
        return {"blocks": []} if kind == "dict" else blocks

    return SimpleNamespace(
        rect=SimpleNamespace(height=height),
        get_textpage=Mock(return_value=object()),
        get_text=Mock(side_effect=get_text),
    )


class PdfExtractionTests(unittest.TestCase):
    """Keep geometry, ordering, empty-page, and read-error contracts explicit."""

    def test_margin_filter_keeps_boundary_and_overlapping_blocks(self) -> None:
        cases = [
            ("within header", 0.0, 99.9, True),
            ("header boundary", 0.0, 100.0, False),
            ("overlapping header", 90.0, 110.0, False),
            ("body", 400.0, 500.0, False),
            ("overlapping footer", 890.0, 910.0, False),
            ("footer boundary", 900.0, 1000.0, False),
            ("within footer", 900.1, 1000.0, True),
        ]
        for name, top, bottom, excluded in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    file_io._is_in_header_or_footer_zone(
                        (0.0, top, 100.0, bottom),
                        1000.0,
                        header_margin=0.1,
                        footer_margin=0.1,
                    ),
                    excluded,
                )

    def test_extracts_kept_blocks_in_supplied_order_and_separates_pages(self) -> None:
        pages = [
            pdf_page(1000.0, [
                text_block("header", 0.0, 99.0),
                text_block("second\n", 300.0, 350.0),
                text_block("first", 100.0, 150.0),
                text_block("footer", 901.0, 1000.0),
            ]),
            pdf_page(1000.0, [text_block("next", 200.0, 250.0)]),
        ]
        with patch.object(file_io.fitz, "open", return_value=nullcontext(pages)):
            text = file_io.extract_text_from_pdf(
                "controlled.pdf", header_margin=0.1, footer_margin=0.1,
            )

        self.assertEqual(text, "second\nfirst next ")

    def test_skips_invalid_height_and_preserves_empty_page_spacing(self) -> None:
        invalid_pages = [pdf_page(0.0, []), pdf_page(-1.0, [])]
        pages = [*invalid_pages, pdf_page(1000.0, [])]
        with patch.object(file_io.fitz, "open", return_value=nullcontext(pages)):
            text = file_io.extract_text_from_pdf(
                "controlled.pdf", header_margin=0.1, footer_margin=0.1,
            )

        self.assertEqual(text, " ")
        for page in invalid_pages:
            page.get_text.assert_not_called()

    def test_open_and_page_errors_become_file_read_errors(self) -> None:
        for stage in ("open", "page"):
            with self.subTest(stage=stage):
                failure = ValueError(f"{stage} failed")
                page = pdf_page(1000.0, [])
                page.get_text.side_effect = failure
                open_kwargs = (
                    {"side_effect": failure} if stage == "open"
                    else {"return_value": nullcontext([page])}
                )
                with patch.object(file_io.fitz, "open", **open_kwargs):
                    with self.assertRaises(file_io.FileReadError) as raised:
                        file_io.extract_text_from_pdf(
                            "broken.pdf", header_margin=0.1, footer_margin=0.1,
                        )

                self.assertIs(raised.exception.__cause__, failure)
                self.assertIn("broken.pdf", str(raised.exception))


class ParagraphExtractionTests(unittest.TestCase):
    """Run real preprocessing while replacing only the document-reading boundary."""

    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / "sample.PDF"
        self.source.touch()

    def paragraphs_from_text(self, text: str | None) -> list[str]:
        """Feed extracted text into the existing public preprocessing path."""
        with patch.object(file_io, "extract_text_from_pdf", return_value=text):
            return file_io.paragraphs_from_input(
                str(self.source), header_margin=0.1, footer_margin=0.2,
            )

    def test_normalizes_whitespace_without_losing_chemical_unicode(self) -> None:
        text = "Alpha\u00a0 \nβ\u200b\tgamma\u202fdelta\u205fepsilon  [M+H]⁺  m/z :123.45"

        self.assertEqual(
            self.paragraphs_from_text(text),
            ["Alpha β gamma delta epsilon [M+H]⁺ m/z: 123.45"],
        )

    def test_joins_isotope_spacing_and_normalizes_colons(self) -> None:
        self.assertEqual(
            self.paragraphs_from_text("HRMS :calcd for C10H12 35Cl:123.4567"),
            ["HRMS: calcd for C10H1235Cl: 123.4567"],
        )

    def test_removes_only_unmatched_brackets(self) -> None:
        self.assertEqual(
            self.paragraphs_from_text("Name) [stable] ((fragment) tail"),
            ["Name [stable] (fragment) tail"],
        )

    def test_splits_before_supported_proton_headers_and_after_last_nmr(self) -> None:
        proton = "1H NMR (400 MHz, CDCl3): δ 7.20 (s, 1H)."
        carbon = "13C NMR (100 MHz, CDCl3): δ 120.0."
        solvent_first = "1H-NMR (DMSO-d6, 500 MHz) δ 8.0 (s, 1H)."

        self.assertEqual(
            self.paragraphs_from_text(
                f"Intro. {proton} {carbon} After. {solvent_first} Tail."
            ),
            ["Intro.", f"{proton} {carbon} ", "After.", f"{solvent_first} ", "Tail."],
        )

    def test_found_decimal_ends_description_after_hrms_instead_of_nmr(self) -> None:
        proton = "1H NMR (400 MHz, CDCl3): δ 7.20 (s, 1H)."

        self.assertEqual(
            self.paragraphs_from_text(
                f"{proton} HRMS calcd 123.4567, found 123.4567 Next compound."
            ),
            [f"{proton} HRMS calcd 123.4567, found 123.4567. ", "Next compound."],
        )

    def test_uses_case_insensitive_pdf_extension_and_forwards_margins(self) -> None:
        with patch.object(file_io, "extract_text_from_pdf", return_value="Text") as reader:
            result = file_io.paragraphs_from_input(
                str(self.source.parent / "unused" / ".." / self.source.name),
                header_margin=0.05, footer_margin=0.15,
            )

        self.assertEqual(result, ["Text"])
        reader.assert_called_once_with(
            str(self.source), header_margin=0.05, footer_margin=0.15,
        )

    def test_docx_reader_uses_same_normalization_path(self) -> None:
        source = self.source.with_suffix(".DOCX")
        document = Document()
        document.add_paragraph("A")
        document.add_paragraph("B")
        document.save(source)
        result = file_io.paragraphs_from_input(
            str(source), header_margin=0.1, footer_margin=0.2,
        )

        self.assertEqual(result, ["A B"])
        self.assertEqual(file_io.extract_text_from_docx(str(source)), "A\nB")

    def test_docx_list_adapter_preserves_empty_and_unreadable_errors(self) -> None:
        source = self.source.with_suffix(".docx")
        Document().save(source)
        with self.assertRaises(file_io.EmptyTextError):
            file_io.paragraphs_from_input(str(source), header_margin=0, footer_margin=0)
        source.write_bytes(b"not a DOCX package")
        with self.assertRaises(file_io.FileReadError):
            file_io.paragraphs_from_input(str(source), header_margin=0, footer_margin=0)

    def test_rejects_missing_paths_and_directories(self) -> None:
        sources = (None, "", " \t", str(self.source.with_name("missing.pdf")), self.directory.name)
        for source in sources:
            with self.subTest(source=source):
                with self.assertRaises(file_io.InputFileNotFoundError):
                    file_io.paragraphs_from_input(
                        source, header_margin=0.1, footer_margin=0.2,
                    )

    def test_rejects_existing_unsupported_file(self) -> None:
        source = self.source.with_suffix(".txt")
        source.touch()
        with self.assertRaises(file_io.UnsupportedFileTypeError):
            file_io.paragraphs_from_input(
                str(source), header_margin=0.1, footer_margin=0.2,
            )

    def test_rejects_empty_reader_results(self) -> None:
        for text in (None, "", " \t\n"):
            with self.subTest(text=text):
                with self.assertRaises(file_io.EmptyTextError):
                    self.paragraphs_from_text(text)

    def test_pdf_with_no_retained_text_is_empty_input(self) -> None:
        cases = [
            ("no pages", []),
            ("empty page", [pdf_page(1000.0, [])]),
            ("header only", [pdf_page(1000.0, [text_block("header", 0.0, 99.0)])]),
        ]
        for name, pages in cases:
            with self.subTest(name=name):
                with patch.object(file_io.fitz, "open", return_value=nullcontext(pages)):
                    with self.assertRaises(file_io.EmptyTextError):
                        file_io.paragraphs_from_input(
                            str(self.source), header_margin=0.1, footer_margin=0.2,
                        )

    def test_preserves_typed_read_errors_and_wraps_unexpected_failures(self) -> None:
        for failure in (file_io.FileReadError("unreadable"), ValueError("unexpected")):
            with self.subTest(failure=type(failure).__name__):
                with patch.object(file_io, "extract_text_from_pdf", side_effect=failure):
                    with self.assertRaises(file_io.FileReadError) as raised:
                        file_io.paragraphs_from_input(
                            str(self.source), header_margin=0.1, footer_margin=0.2,
                        )

                if isinstance(failure, file_io.FileReadError):
                    self.assertIs(raised.exception, failure)
                else:
                    self.assertIs(raised.exception.__cause__, failure)


if __name__ == "__main__":
    unittest.main()
