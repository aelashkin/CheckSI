"""Continuation ownership at real DOCX paragraphs and Unicode PDF lines."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from docx import Document

from checksi.controller import CheckSIController
from checksi.document_parser import DocumentParser
from checksi.file_io import _join_formula_continuations, _source_name_starts
from tests.test_assessment_coverage import save_pdf
from tests.test_formula_reconciliation import EA_ETHANOL, EA_PROPANOL, HRMS, NMR
from tests.test_name_resolver import StubWorker, make_config


class FormulaContinuationOwnershipTests(unittest.TestCase):
    def evaluate(self, parts, extension="docx"):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ("input." + extension)
            if extension == "docx":
                document = Document()
                for part in parts:
                    document.add_paragraph(part)
                document.save(path)
            else:
                save_pdf(path, "\n".join(parts))
            parsed = DocumentParser().parse(str(path), header_margin=0, footer_margin=0)
        worker = StubWorker(lambda backend, name: {
            "propan-1-ol": "CCCO", "ethanol": "CCO",
            "N-methylpropan-1-amine": "CCCNC",
            "N,N'-dimethylethane-1,2-diamine": "CNCCNC",
            "propan-2-amine": "CC(N)C",
        }.get(name))
        with patch("checksi.analysis.name_resolver._ResolverWorker", return_value=worker):
            result = CheckSIController(make_config()).run_analysis(parsed.paragraphs)
        return parsed, result, worker.calls

    def test_separate_mixed_formula_paragraphs_join_in_either_order(self):
        for observation in ("obsd", "measured", ""):
            hrms = HRMS.replace("found 61.0648", observation + " 61.0648")
            ea = EA_PROPANOL.replace("Found:", observation + ":")
            for blocks in ((hrms, ea), (ea, hrms)):
                for extension in ("docx", "pdf"):
                    with self.subTest(observation=observation, blocks=blocks, extension=extension):
                        _, result, _ = self.evaluate(
                            ["propan-1-ol (1)", NMR, " ".join(blocks)], extension)
                        first, = result["compounds"]
                        self.assertEqual(first["formula_sources"]["hrms"]["formula"], "C3H9O")
                        self.assertEqual(first["formula_sources"]["ea"]["formula"], "C3H8O")
                        self.assertEqual(result["error_stats"]["formula_not_found"], 0)
                        self.assertFalse(any(item["reason"] == "no_supported_proton_header"
                                             for item in result["assessment"]["unassessed"]))

    def test_qtof_and_polarity_prefixes_retain_independent_formula_comparison(self):
        prefixes = ("HRMS (ESI-QTOF)", "HRMS (ESI-Q-TOF)", "HRMS (Q-TOF)",
                    "HRMS (ESI/QTOF)", "(+)-ESI-HRMS", "UPLC-HRMS", "UHPLC-HRMS",
                    "QTOF MS", "Q-TOF MS", "ESI-QTOF", "ESI-Q-TOF")
        for prefix in prefixes:
            for extension in ("docx", "pdf"):
                with self.subTest(prefix=prefix, extension=extension):
                    _, result, _ = self.evaluate(
                        ["propan-1-ol (1)", NMR + EA_ETHANOL,
                         HRMS.replace("HRMS (ESI)", prefix)], extension)
                    first, = result["compounds"]
                    self.assertTrue(first["formula_sources"]["hrms_neutral"]["usable"])
                    self.assertEqual(result["error_stats"]["formula_source_mismatch"], 1)

    def test_next_physical_name_and_properties_preserve_previous_source_and_findings(self):
        layouts = (
            ["ethanol (1.76): colorless oil (50 mg, 80%)."],
            ["ethanol (1.76). Colorless oil (50 mg, 80%)."],
            ["ethanol (1.76); White solid (50 mg, 80%)."],
            ["ethanol (1.76) Colorless oil (50 mg, 80%)."],
            ["ethanol (1.76).Colorless oil (50 mg, 80%)."],
            ["ethanol (1.76)Colorless oil (50 mg, 80%)."],
            ["ethanol (1.76) was obtained as a colorless oil (50 mg, 80%)."],
            ["ethanol (1.76)", "Colorless oil (50 mg, 80%)."],
            ["N-methylpropan-1-amine (1.76)", "White solid; mp 120-122 °C."],
            ["N,N'-dimethylethane-1,2-diamine (1.76)", "Colorless oil."],
            ["N,N'-dimethylethane-1,2-diamine"],
            ["propan-2-amine"],
            ["N,N'-dimethyl", "ethane-1,2-diamine (1.76)", "Colorless oil."],
            ["N,N'-dimethyl", "ethane-1,2-diamine (1.76). White solid."],
        )
        for block in ("HRMS [M+H]+ calcd for C3H9O+",
                      "HRMS [M+H]+ calcd for C3H9O+ 61.0648",
                      "Anal. Calcd for C3H8O"):
            for extension in ("docx", "pdf"):
                _, baseline, _ = self.evaluate(["propan-1-ol (1.75)", NMR + block], extension)
                previous, = baseline["compounds"]
                for layout in layouts:
                    with self.subTest(block=block, extension=extension, layout=layout):
                        _, result, calls = self.evaluate(
                            ["propan-1-ol (1.75)", NMR + block, *layout, NMR + EA_ETHANOL],
                            extension)
                        first, second = result["compounds"]
                        self.assertEqual(first["original_text"].strip(), previous["original_text"].strip())
                        self.assertEqual(first["formula_sources"], previous["formula_sources"])
                        self.assertEqual(first["all_warnings"], previous["all_warnings"])
                        self.assertEqual(first["name"], "propan-1-ol")
                        if block.startswith("HRMS"):
                            self.assertTrue(any("hrms_found_mass_missing." in text
                                                for text in first["all_warnings"]))
                            if "61.0648" not in block:
                                self.assertTrue(any("hrms_calcd_mass_missing." in text
                                                    for text in first["all_warnings"]))
                        self.assertNotIn("HRMS", second["name"])
                        self.assertNotIn("Calcd", second["name"])
                        self.assertTrue(all("HRMS" not in name and "Calcd" not in name
                                            for _, name in calls))

    def test_inline_property_separator_cannot_turn_data_or_quantities_into_names(self):
        fragments = (
            "Sample (50 mg). Colorless oil.",
            "1.20 (3H); White solid.",
            "63.0 (1C) Colorless oil.",
            "13C NMR (100 MHz, CDCl3): δ 63.0 (A). White solid.",
            "(1.76). Colorless oil.",
        )
        for fragment in fragments:
            with self.subTest(fragment=fragment):
                self.assertEqual(_source_name_starts([NMR + HRMS, fragment, NMR]), set())
                for extension in ("docx", "pdf"):
                    _, _, calls = self.evaluate(
                        ["propan-1-ol (1.75)", NMR + HRMS, fragment, NMR + EA_ETHANOL], extension)
                    self.assertTrue(all(name == "propan-1-ol" for _, name in calls))
        self.assertEqual(_source_name_starts(
            [NMR + HRMS, "(ethanol (1.76). Colorless oil.", NMR]), set())
        for tail in ("C3H8O(H2O). White solid.", "C3H8O(H2O); White solid.",
                     "C3H8O(H2O) Colorless oil."):
            with self.subTest(formula_tail=tail):
                self.assertEqual(_source_name_starts([NMR + "Anal. Calcd for", tail, NMR]), set())

    def test_wrapped_supported_and_opaque_formula_tails_remain_owned(self):
        for block, formula_parts in (("HRMS [M+H]+ calcd for", ["C3H9O+"]),
                                     ("HRMS [M+H]+ calcd for", ["C3H9O(H2O)+"]),
                                     ("Anal. Calcd for", ["C3H8O(H2O)"]),
                                     ("Anal. Calcd for", ["C3H8O", "hydrochloride"])):
            for extension in ("docx", "pdf"):
                with self.subTest(block=block, tail=formula_parts, extension=extension):
                    _, baseline, _ = self.evaluate(
                        ["propan-1-ol (1.75)", NMR + block, *formula_parts], extension)
                    _, result, _ = self.evaluate(
                        ["propan-1-ol (1.75)", NMR + block, *formula_parts,
                         "ethanol (1.76)", NMR + EA_ETHANOL], extension)
                    previous, = baseline["compounds"]
                    first, second = result["compounds"]
                    self.assertEqual(first["formula_sources"], previous["formula_sources"])
                    self.assertEqual(first["all_warnings"], previous["all_warnings"])
                    literal = " ".join(formula_parts)
                    self.assertIn(literal, first["original_text"])
                    source = first["formula_sources"]["ea" if block.startswith("Anal") else "hrms"]
                    self.assertEqual(source["candidate"]["raw"], literal.removesuffix("+"))
                    if "(" in literal or "hydrochloride" in literal:
                        self.assertEqual(source["status"], "unsupported")
                    self.assertEqual(second["name"], "ethanol")

    def test_wrapped_opaque_formula_and_property_text_stay_with_their_declaration(self):
        for header, tail in (("Anal. Calcd for", "C3H8O(H2O). White solid."),
                             ("HRMS [M+H]+ calcd for", "C3H9O(H2O)+; White solid.")):
            for extension in ("docx", "pdf"):
                with self.subTest(header=header, tail=tail, extension=extension):
                    source_parts = ["propan-1-ol (1.75)", NMR + header, tail]
                    _, baseline, _ = self.evaluate(source_parts, extension)
                    _, result, calls = self.evaluate(
                        [*source_parts, "ethanol (1.76). Colorless oil.", NMR + EA_ETHANOL], extension)
                    previous, = baseline["compounds"]
                    first, second = result["compounds"]
                    self.assertEqual(first["original_text"].strip(), previous["original_text"].strip())
                    self.assertEqual(first["formula_sources"], previous["formula_sources"])
                    self.assertEqual(first["all_warnings"], previous["all_warnings"])
                    self.assertTrue(all("calcd" not in name.lower() for _, name in calls))
                    self.assertNotIn("calcd", second["name"].lower())

    def test_property_prefixes_need_real_formula_data(self):
        for property_text in ("IR (ATR): 1650 cm-1.", "FTIR: 1650 cm-1.",
                              "FT-IR: 1650 cm-1.", "mp 120-122 °C.", "Rf 0.3."):
            with self.subTest(property=property_text):
                paragraph = property_text + " " + HRMS
                self.assertEqual(_join_formula_continuations([NMR + EA_ETHANOL, paragraph]),
                                 [NMR + EA_ETHANOL + " " + paragraph])
        for caption in ("HRMS spectra: Figure S12", "QTOF MS spectra: Figure S12",
                        "(+)-ESI-HRMS measurements were not possible", "UPLC-HRMS chromatogram S12"):
            with self.subTest(caption=caption):
                self.assertEqual(_join_formula_continuations([NMR + EA_ETHANOL, caption]),
                                 [NMR + EA_ETHANOL, caption])

    def test_flattened_trailing_name_is_not_joined(self):
        tail = "APCI [M+H]+ calcd for C3H9O+ ethanol (1.76)"
        self.assertEqual(_join_formula_continuations([NMR, tail, NMR + EA_ETHANOL]),
                         [NMR, tail, NMR + EA_ETHANOL])

    def test_real_flattened_pdf_trailing_name_is_not_joined(self):
        tail = "APCI [M+H]+ calcd for C3H9O+ ethanol (1.76)"
        _, result, calls = self.evaluate(
            ["propan-1-ol (1.75) " + NMR + tail + " " + NMR + EA_ETHANOL], "pdf")
        first, second = result["compounds"]
        self.assertNotIn("ethanol", first["original_text"])
        self.assertEqual(result["error_stats"]["formula_not_found"], 1)
        self.assertFalse(any("APCI" in name for _, name in calls))
        self.assertNotIn("APCI", second["name"])

    def test_truncated_formula_without_a_terminal_heading_stays_with_its_owner(self):
        tail = "APCI [M+H]+ calcd for C3H9O+"
        for extension in ("docx", "pdf"):
            with self.subTest(extension=extension):
                _, result, calls = self.evaluate(
                    ["propan-1-ol (1.75)", NMR + tail, "ethanol (1.76)", NMR + EA_ETHANOL], extension)
                first, second = result["compounds"]
                self.assertIn(tail, first["original_text"])
                self.assertNotIn("ethanol", first["original_text"])
                self.assertEqual((second["name"], second["identifier"]), ("ethanol", "1.76"))
                self.assertFalse(any("APCI" in name for _, name in calls))

    def test_repeated_kinds_and_nmr_stay_separate_and_visible(self):
        mixed = HRMS + EA_PROPANOL
        for previous, continuation in ((NMR + HRMS, HRMS),
                                       (NMR + EA_PROPANOL, EA_PROPANOL),
                                       (NMR + HRMS, mixed), (NMR, HRMS + NMR),
                                       (NMR + HRMS, HRMS.replace("HRMS (ESI)", "QTOF MS")),
                                       (NMR + HRMS.replace("HRMS (ESI)", "QTOF MS"), HRMS)):
            with self.subTest(previous=previous, continuation=continuation):
                self.assertEqual(_join_formula_continuations([previous, continuation]),
                                 [previous, continuation])
        _, result, _ = self.evaluate(["propan-1-ol (1)", NMR + HRMS, HRMS])
        self.assertTrue(any(item["reason"] == "no_supported_proton_header"
                            for item in result["assessment"]["unassessed"]))

    def test_a_continuation_requires_a_preceding_supported_proton_header(self):
        for previous in ("Colourless oil.", "13C NMR (100 MHz, CDCl3): δ 10.0."):
            with self.subTest(previous=previous):
                self.assertEqual(_join_formula_continuations([previous, HRMS]), [previous, HRMS])


if __name__ == "__main__":
    unittest.main()
