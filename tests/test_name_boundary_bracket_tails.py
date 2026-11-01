"""Retain incomplete bracket-led signal descriptors with their source spectrum."""

from importlib.resources import files
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from docx import Document
import fitz

from checksi.analysis import name_resolver
from checksi.config_schema import ErrorSeverity
from checksi.controller import CheckSIController
from checksi.document_parser import DocumentParser
from checksi.file_io import paragraphs_from_input
from checksi.utils import name_boundaries
from tests.test_name_boundary_integrity import NMR
from tests.test_name_resolver import StubWorker, make_config
from tests.test_wave3_regressions import NAME, PROTON


class BracketTailSourceTests(unittest.TestCase):
    def parse(self, parts, extension):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / f"input.{extension}"
            if extension == "docx":
                document = Document()
                for part in parts:
                    document.add_paragraph(part)
                document.save(source)
            else:
                with fitz.open() as document:
                    page = document.new_page()
                    remaining = page.insert_textbox(
                        fitz.Rect(40, 40, 555, 800), "\n".join(parts), fontsize=9,
                        fontname="Noto", fontfile=str(files("checksi").joinpath(
                            "resources/fonts/NotoSans-Regular.ttf")),
                    )
                    self.assertGreaterEqual(remaining, 0)
                    document.save(source)
                with fitz.open(source) as document:
                    extracted = document[0].get_text()
                self.assertIn("δ", extracted)
                self.assertNotIn("\x00", extracted)
                if "°C" in " ".join(parts):
                    self.assertIn("°C", extracted)
            parsed = DocumentParser().parse(str(source), header_margin=0, footer_margin=0)
            self.assertEqual(parsed.paragraphs, paragraphs_from_input(str(source), header_margin=0, footer_margin=0))
            return parsed.paragraphs

    def analyze(self, paragraphs, mode, severity):
        config = make_config(mode=mode)
        config.errors.default_severity = ErrorSeverity(severity)
        worker = StubWorker(lambda backend, name: {"propan-1-ol": "CCCO", "ethanol": "CCO"}.get(name))
        with patch.object(name_resolver, "_ResolverWorker", return_value=worker):
            result = CheckSIController(config, event_dispatcher=lambda _: None).run_analysis(paragraphs)
        return result, worker.calls

    def test_incomplete_bracket_tails_preserve_previous_text_results_and_findings(self):
        carbon = NMR.replace(". 13C", "; 13C").removesuffix(". ")
        proton = PROTON.removesuffix("1.20 (s, 3H).") + "1.20"
        for label, header, tail in (
            ("carbon", carbon, "(s); mp 52-54 °C"),
            ("carbon", carbon, "(s)"),
            ("proton", proton, "(s, OH); mp 52-54 °C"),
            ("carbon", carbon, "(z); mp 52-54 °C"),
            ("carbon", carbon, "(br. s)"),
        ):
            for extension in ("docx", "pdf"):
                parts = ["propan-1-ol (1)", header, tail]
                baseline_paragraphs = self.parse(parts, extension)
                paragraphs = self.parse(parts + ["ethanol (2)", PROTON], extension)
                for mode in ("off", "on"):
                    for severity in ("warning", "disabled"):
                        with self.subTest(tail=tail, extension=extension, mode=mode, severity=severity):
                            baseline, _ = self.analyze(baseline_paragraphs, mode, severity)
                            result, calls = self.analyze(paragraphs, mode, severity)
                            previous, = baseline["compounds"]
                            first, second = result["compounds"]
                            self.assertIn(tail, previous["original_text"])
                            self.assertEqual(first["original_text"].strip(), previous["original_text"].strip())
                            self.assertEqual(first["formula_sources"], previous["formula_sources"])
                            self.assertEqual(first["all_warnings"], previous["all_warnings"])
                            if label == "carbon" and "; mp" in tail:
                                self.assertTrue(any("C NMR Result: 3" in text for text in first["all_warnings"]))
                                self.assertFalse(any("c13_section_missing." in text for text in first["all_warnings"]))
                            elif label == "proton" and severity != "disabled":
                                for code in ("hnmr_h_count_missing", "hnmr_signal_desc_invalid"):
                                    self.assertTrue(any(code + "." in text for text in first["all_warnings"]))
                            self.assertEqual((second["name"], second["identifier"]), ("ethanol", "2"))
                            self.assertTrue(all(name in {"propan-1-ol", "ethanol"} for _, name in calls))
                            if mode == "off":
                                self.assertEqual(calls, [])

    def test_descriptor_only_bracket_requires_a_preceding_spectral_shift(self):
        for fragment in ("(s)", "(s, OH); mp 52-54 °C", "(z)", "(br. s)"):
            with self.subTest(fragment=fragment):
                parts = [PROTON, fragment, "ethanol (2)", PROTON]
                self.assertIsNone(name_boundaries.compound_name_start(parts, 2))
                self.assertEqual(name_boundaries.compound_name_starts(parts), set())
                for extension in ("docx", "pdf"):
                    paragraphs = self.parse(["propan-1-ol (1)", *parts], extension)
                    for mode in ("off", "on"):
                        for severity in ("warning", "disabled"):
                            result, calls = self.analyze(paragraphs, mode, severity)
                            first, second = result["compounds"]
                            self.assertEqual(second["name"], "")
                            self.assertEqual(second["name_resolution"]["reason"], "mixed_context")
                            self.assertTrue(all(name == "propan-1-ol" for _, name in calls))

    def test_chemical_stereodescriptors_and_parentheses_remain_name_wraps(self):
        for fragment, rest in (("(R)-1-phenyl", "ethanol"), ("(S)-1-phenyl", "ethanol"),
                               ("(E)-", "but-2-ene"), ("(Z)-", "but-2-ene"),
                               ("(1R,3S)-", "cyclopentane-1,3-diamine"),
                               ("(4-methoxyphenyl)-", "methanol"), ("(tert-butyl)-", "benzene"),
                               ("(m)-tolyl", "methanol")):
            for extension in ("docx", "pdf"):
                with self.subTest(fragment=fragment, extension=extension):
                    name = fragment + " " + rest
                    paragraphs = self.parse(["propan-1-ol (1)", NMR.removesuffix(". "), fragment,
                                             rest + " (2a)", PROTON], extension)
                    worker = StubWorker(lambda backend, candidate: "CCO" if candidate in {"propan-1-ol", name} else None)
                    with patch.object(name_resolver, "_ResolverWorker", return_value=worker):
                        result = CheckSIController(make_config(), event_dispatcher=lambda _: None).run_analysis(paragraphs)
                    first, second = result["compounds"]
                    self.assertEqual((second["name"], second["identifier"]), (name, "2a"))
                    self.assertNotIn(name, first["original_text"])
                    self.assertTrue(all(candidate in {"propan-1-ol", name} for _, candidate in worker.calls))

    def test_stereo_prefixed_complete_source_name_is_not_a_descriptor_tail(self):
        for stereo in ("(R)", "(S)", "(E)", "(Z)", "(1R,3S)"):
            for extension in ("docx", "pdf"):
                with self.subTest(stereo=stereo, extension=extension):
                    name = stereo + " 1-phenylethanol"
                    paragraphs = self.parse(["propan-1-ol (1)", NMR.removesuffix(". "), name + " (2a)", PROTON], extension)
                    worker = StubWorker(lambda backend, candidate: "CCO" if candidate in {"propan-1-ol", name} else None)
                    with patch.object(name_resolver, "_ResolverWorker", return_value=worker):
                        result = CheckSIController(make_config(), event_dispatcher=lambda _: None).run_analysis(paragraphs)
                    first, second = result["compounds"]
                    self.assertEqual((second["name"], second["identifier"]), (name, "2a"))
                    self.assertNotIn(name, first["original_text"])
                    self.assertTrue(all(candidate in {"propan-1-ol", name} for _, candidate in worker.calls))

    def test_unknown_bracket_tails_are_refused_without_detaching_previous_data(self):
        for tail in ("(multiplet); mp 52-54 °C", "(not-resolved); mp 52-54 °C", "[unresolved]; mp 52-54 °C"):
            for extension in ("docx", "pdf"):
                parts = ["propan-1-ol (1)", NMR.replace(". 13C", "; 13C").removesuffix(". "), tail]
                baseline_paragraphs = self.parse(parts, extension)
                paragraphs = self.parse(parts + ["ethanol (2)", PROTON], extension)
                for mode in ("off", "on"):
                    for severity in ("warning", "disabled"):
                        with self.subTest(tail=tail, extension=extension, mode=mode, severity=severity):
                            baseline, _ = self.analyze(baseline_paragraphs, mode, severity)
                            result, calls = self.analyze(paragraphs, mode, severity)
                            previous, = baseline["compounds"]
                            first, second = result["compounds"]
                            self.assertIn(previous["original_text"].strip(), first["original_text"])
                            self.assertEqual(first["all_warnings"], previous["all_warnings"])
                            self.assertEqual(first["formula_sources"], previous["formula_sources"])
                            self.assertEqual(second["name"], "")
                            self.assertTrue(all(name == "propan-1-ol" for _, name in calls))

    def test_attached_chemical_bracket_names_keep_the_complete_name(self):
        for name in ("(4-methoxyphenyl)methanol", "(tert-butyl)benzene", "[2.2]paracyclophane"):
            for extension in ("docx", "pdf"):
                with self.subTest(name=name, extension=extension):
                    paragraphs = self.parse(["propan-1-ol (1)", NMR.removesuffix(". "), name + " (2a)", PROTON], extension)
                    worker = StubWorker(lambda backend, candidate: "CCO" if candidate in {"propan-1-ol", name} else None)
                    with patch.object(name_resolver, "_ResolverWorker", return_value=worker):
                        result = CheckSIController(make_config(), event_dispatcher=lambda _: None).run_analysis(paragraphs)
                    first, second = result["compounds"]
                    self.assertEqual((second["name"], second["identifier"]), (name, "2a"))
                    self.assertNotIn(name, first["original_text"])
                    self.assertTrue(all(candidate in {"propan-1-ol", name} for _, candidate in worker.calls))

    def test_report_peak_list_is_refused_without_dispatching_a_name_suffix(self):
        for extension in ("docx", "pdf"):
            paragraphs = self.parse(["Duplicates found in HNMR: ['2.22'] " + NAME + " (5.1)", PROTON], extension)
            for mode in ("off", "on"):
                with self.subTest(extension=extension, mode=mode):
                    result, calls = self.analyze(paragraphs, mode, "warning")
                    compound, = result["compounds"]
                    self.assertEqual(compound["name"], "")
                    self.assertEqual(compound["name_resolution"]["reason"], "mixed_context")
                    self.assertEqual(calls, [])
