"""Symbol-font Greek, decimal spectrometer frequencies and found-mass periods in PDF input."""

from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

import fitz

from checksi import file_io, source_annotations
from checksi.assessment import SUPPORTED_PROTON_HEADER
from checksi.source_annotations import _description_occurrences, _normalized


HEADER = "1H NMR (400 MHz, CDCl3): "
PROTON_LINE = "1H NMR (400 MHz, CDCl3): δ 1.20 (d, J = 7.0 Hz, 6H)."
APPLE_SYMBOLS = Path("/System/Library/Fonts/Apple Symbols.ttf")


def span_page(text: str, font: str) -> MagicMock:
    """Build a one-span page whose dict/rawdict output carries the given font."""
    bbox = (72.0, 90.0, 400.0, 102.0)
    chars = [{"c": c, "bbox": (72.0 + i, 90.0, 73.0 + i, 102.0)} for i, c in enumerate(text)]
    span = {"text": text, "font": font, "chars": chars}
    block = {"type": 0, "number": 0, "bbox": bbox, "lines": [{"bbox": bbox, "spans": [span]}]}
    page = MagicMock(number=0)
    page.rect.height = 800.0
    page.get_text.return_value = {"blocks": [block]}
    return page


def save_symbol_delta_pdf(path: Path) -> None:
    """Write a page whose δ is a Symbol-font "d", as in many journal SI files."""
    with fitz.open() as document:
        page = document.new_page()
        page.insert_text((72, 100), HEADER, fontname="helv", fontsize=11)
        x = 72 + fitz.get_text_length(HEADER, fontname="helv", fontsize=11)
        page.insert_text((x, 100), "d", fontname="symb", fontsize=11)
        page.insert_text((x + 8, 100), " 1.20 (d, J = 7.0 Hz, 6H).", fontname="helv", fontsize=11)
        document.save(path)


class SymbolFontTests(unittest.TestCase):
    """Greek letters encoded through the Symbol font become Unicode Greek."""

    def test_maps_latin_letters_only_in_symbol_spans(self) -> None:
        self.assertEqual(file_io.symbol_span_text("d", "Symbol"), "δ")
        self.assertEqual(file_io.symbol_span_text("d", "SymbolMT"), "δ")
        self.assertEqual(file_io.symbol_span_text("d", "Helvetica"), "d")
        self.assertEqual(file_io.symbol_span_text("(d, J = 7.0 Hz)", "TimesNewRoman"), "(d, J = 7.0 Hz)")

    def test_unicode_fonts_named_symbol_are_not_remapped(self) -> None:
        for font in ("AppleSymbols", "ABCDEF+AppleSymbols", "SegoeUISymbol",
                     "STIXSymbols", "NotoSansSymbols-Regular"):
            with self.subTest(font=font):
                self.assertEqual(file_io.symbol_span_text(PROTON_LINE, font), PROTON_LINE)

    def test_legacy_symbol_aliases_are_remapped(self) -> None:
        for font in ("Symbol", "SymbolMT", "ABCDEF+SymbolMT", "Symbol,Bold",
                     "Symbol-Italic", "SymbolPS"):
            with self.subTest(font=font):
                self.assertEqual(file_io.symbol_span_text("d", font), "δ")

    def test_unicode_symbol_font_block_is_not_changed_by_extraction(self) -> None:
        page = span_page(PROTON_LINE, "AppleSymbols")

        self.assertEqual(file_io._symbol_block_texts(page, object()), {})

    def test_unicode_symbol_font_text_keeps_its_source_association(self) -> None:
        page = span_page(PROTON_LINE, "AppleSymbols")
        document = MagicMock(page_count=1)
        document.__iter__.return_value = iter([page])
        document.__getitem__.return_value = page
        compound = {"id": 1, "original_text": PROTON_LINE, "highlight_info": {"hnmr_text": PROTON_LINE}}

        with patch.object(source_annotations.fitz, "open", return_value=nullcontext(document)):
            result = source_annotations.annotate_source("source.pdf", Path("out.pdf"), [compound], "summary")

        self.assertEqual(result["highlighted_compounds"], 1)
        self.assertEqual(result["annotation_ambiguities"], [])

    @unittest.skipUnless(APPLE_SYMBOLS.exists(), "Apple Symbols font is only available on macOS")
    def test_pdf_text_set_in_apple_symbols_is_extracted_unchanged(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "apple_symbols.pdf"
            with fitz.open() as document:
                page = document.new_page()
                page.insert_font(fontname="F0", fontfile=str(APPLE_SYMBOLS))
                page.insert_text((72, 100), PROTON_LINE, fontname="F0", fontsize=11)
                document.save(source)
            text = file_io.extract_text_from_pdf(str(source), header_margin=0, footer_margin=0)

        self.assertEqual(text.strip(), PROTON_LINE)
        self.assertIsNotNone(SUPPORTED_PROTON_HEADER.search(text))

    def test_maps_symbol_private_use_glyphs_in_any_font(self) -> None:
        self.assertEqual(file_io.symbol_span_text("", "Helvetica"), "δ")
        self.assertEqual(file_io.symbol_span_text("", ""), "αβ")

    def test_pdf_extraction_reads_symbol_delta_and_keeps_latin_multiplicity(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "symbol.pdf"
            save_symbol_delta_pdf(source)
            text = file_io.extract_text_from_pdf(str(source), header_margin=0, footer_margin=0)

        self.assertIn("CDCl3): δ", text)
        self.assertIn("(d, J = 7.0 Hz, 6H)", text)
        self.assertIsNotNone(SUPPORTED_PROTON_HEADER.search(text))


class DecimalFrequencyTests(unittest.TestCase):
    """A decimal spectrometer frequency is an admitted proton header."""

    def test_decimal_frequency_header_starts_a_record(self) -> None:
        for header in ("1H NMR (400.13 MHz, CDCl3): δ", "1H NMR (CDCl3, 400.13 MHz): δ"):
            with self.subTest(header=header):
                self.assertIsNotNone(SUPPORTED_PROTON_HEADER.search(f"{header} 7.26 (s, 1H)."))
                paragraphs = file_io._paragraphs_from_text(
                    f"Methyl ester 3a was obtained. {header} 7.26 (s, 1H).", "sample.pdf",
                )
                self.assertTrue(any(p.startswith(header) for p in paragraphs), paragraphs)


class FoundPeriodAnnotationTests(unittest.TestCase):
    """Source matching tolerates the period that analysis adds after "found <mass>"."""

    def test_inserted_period_after_found_mass_is_optional(self) -> None:
        source = _normalized("MS (ESI) m/z: [M]+ Calcd for C14H9NOS2 271.01; Found 271.05; Anal.")
        description = _normalized("MS (ESI) m/z: [M]+ Calcd for C14H9NOS2 271.01; Found 271.05.; Anal.")

        hits = _description_occurrences(source, description)

        self.assertEqual(len(hits), 1)
        self.assertEqual(source[slice(*hits[0])], source)

    def test_exact_match_is_used_when_the_period_is_in_the_source(self) -> None:
        text = _normalized("HRMS calcd 47.0491, found 47.0491.")

        self.assertEqual(_description_occurrences(text, text), [(0, len(text))])
