"""Coverage follows real analyzer selections and survives unsupported DOCX XML."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml.etree import Element

from checksi import assessment, controller
from checksi.config_schema import AppConfig
from checksi.assessment import analytical_excerpt
from checksi.controller import CheckSIController
from checksi.document_parser import DocumentParser
from checksi.analysis.hrms_fields import protect_ea_component_separators
from checksi.utils.text_processing import replace_with_offsets


PROTON = "1H NMR (400 MHz, CDCl3): δ 1.00 (s, 6H)."
CARBON = "13C NMR (100 MHz, CDCl3): δ 20.0."
EA = "Anal. Calcd for C2H6O: C, 52.14; H, 13.13. Found: C, 40.00; H, 13.13."
HRMS = "HRMS (ESI): calcd for C2H7O+ [M+H]+ 47.0491, found 47.0491."


class AssessmentBoundaryTests(unittest.TestCase):
    def analyze(self, text, severity="critical", source_omissions=()):
        config = AppConfig.model_validate({
            "paths": {"input_path": "input.pdf", "output_dir": "."},
            "modes": {"deep_name_analysis": "off", "output_mode": "essential"}, "solvents": {"entries": []},
            "errors": {"default_severity": severity},
        })
        paragraphs = text if isinstance(text, list) else [text]
        return CheckSIController(config, event_dispatcher=lambda _: None).run_analysis(
            paragraphs, source_omissions=source_omissions)

    def test_consumed_ea_and_hrms_before_proton_are_assessed_with_findings_enabled(self):
        text = " ".join((EA, HRMS, PROTON, CARBON))
        enabled, disabled = self.analyze(text), self.analyze(text, "disabled")
        self.assertEqual(enabled["assessment"]["status"], "complete")
        self.assertGreater(enabled["error_stats"]["ea_tolerance_exceeded"], 0)
        self.assertEqual(enabled["assessment"], disabled["assessment"])
        self.assertTrue(all(value == 0 for value in disabled["error_stats"].values()))

    def test_unmatched_ea_and_formula_less_hrms_remain_unassessed(self):
        for block in (EA.replace("Calcd for", "Calcd (%) for"), "HRMS (ESI): found 47.0491."):
            with self.subTest(block=block):
                result = self.analyze(" ".join((PROTON, CARBON, block)))
                self.assertEqual(result["assessment"]["status"], "partial")
                self.assertIn("additional_analysis_section", [r["reason"] for r in result["assessment"]["unassessed"]])

    def test_mass_coverage_follows_the_normalized_text_read_by_the_hrms_check(self):
        # BArF expands to a longer formula before the HRMS check, so offsets in
        # the analyzed text differ from the extracted paragraph after a label.
        labelled_proton = "1H NMR (400 MHz, CDCl3): δ 7.70 (s, 8H, BArF), 7.52 (s, 4H, BArF), 1.00 (s, 6H)."
        for proton in (PROTON, labelled_proton):
            for block in ("HRMS (ESI): m/z [M]– calcd for BArF 863.0654, found 863.0650.",
                          "HRMS: calcd for BArF– [M]– 863.0654, found 863.0650."):
                with self.subTest(proton=proton, block=block):
                    text = " ".join((proton, CARBON, block))
                    self.assertEqual(self.analyze(text, "disabled")["assessment"]["status"], "complete")
                    repeated = text + " " + block
                    omission, = self.analyze(repeated, "disabled")["assessment"]["unassessed"]
                    location = omission["source"]
                    self.assertEqual(location["start_offset"], repeated.rindex(block))
                    self.assertEqual(repeated[location["start_offset"]:location["end_offset"]], block)

    def test_mass_coverage_text_is_the_text_passed_to_the_ea_and_hrms_checks(self):
        text = " ".join(("1H NMR (400 MHz, CDCl3): δ 7.70 (s, 8H, BArF), 1.00 (s, 6H).", CARBON,
                         EA.replace("C2H6O", "C2H6O•BArF"),
                         "HRMS (ESI): m/z [M•]+. calcd for BArF– [M]– 863.0654, found 863.0650."))
        with (patch.object(controller, "_check_hrms", wraps=controller._check_hrms) as hrms,
              patch.object(controller, "_check_elemental_analysis",
                           wraps=controller._check_elemental_analysis) as ea):
            self.analyze(text, "disabled")
        protected = protect_ea_component_separators(text)
        self.assertEqual(protected, text.replace('C2H6O•BArF', 'C2H6O·BArF'))
        mass_text, origin = replace_with_offsets(protected, assessment._MASS_TEXT_REPLACEMENTS)
        self.assertNotEqual(mass_text, text)
        self.assertEqual(hrms.call_args.args[2], mass_text)
        self.assertEqual(ea.call_args.args[2], mass_text)
        self.assertEqual((len(origin), origin[-1]), (len(mass_text) + 1, len(text)))

    def test_coverage_selects_the_proton_section_actually_analyzed(self):
        first = PROTON.replace("400 MHz", "400.13 MHz")
        text = " ".join((first, PROTON, CARBON))
        result = self.analyze(text)
        self.assertIn("400.13", result["compounds"][0]["highlight_info"]["hnmr_text"])
        omission, = result["assessment"]["unassessed"]
        self.assertEqual(omission["source"]["start_offset"], text.index(PROTON))

    def test_mass_prose_cannot_borrow_a_formula_from_the_next_sentence(self):
        for prose in ("HRMS (ESI) was used. A C18 column was selected.",
                      "HRMS spectra were recorded. C2H6O was used as a solvent.",
                      "HRMS spectra were recorded. LCMS samples contained C2H6O.",
                      "HRMS spectra were recorded [12]. C2H6O was used as a solvent."):
            with self.subTest(prose=prose):
                self.assertIsNone(analytical_excerpt(prose))
                result = self.analyze(prose + " " + PROTON + " " + CARBON)
                self.assertEqual(result["assessment"]["status"], "complete")

    def test_period_after_an_ion_label_keeps_formula_less_mass_data_a_candidate(self):
        for block in ("HRMS (ESI): m/z [M+H]+. Calcd: 149.0961. Found: 149.1960.",
                      "HRMS (ESI): m/z [M–H]–. Calcd: 147.0815. Found: 147.0810.",
                      "HRMS (EI): m/z M+. Calcd: 148.0883. Found: 148.0880."):
            with self.subTest(block=block):
                self.assertIsNotNone(analytical_excerpt(block))
                omission, = self.analyze(" ".join((PROTON, CARBON, block)))["assessment"]["unassessed"]
                self.assertEqual(omission["reason"], "additional_analysis_section")
                self.assertEqual(omission["excerpt"], block)

    def test_bad_table_attribute_keeps_body_analysis_and_explicit_inspection_failure(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "malformed-table.docx"
            doc = Document()
            doc.add_paragraph(PROTON + " " + CARBON)
            cell = doc.add_table(rows=1, cols=1).cell(0, 0)
            cell.text = CARBON
            span = OxmlElement("w:gridSpan")
            span.set(qn("w:val"), "two")
            cell._tc.get_or_add_tcPr().append(span)
            doc.save(path)
            parsed = DocumentParser().parse(str(path), header_margin=0, footer_margin=0)
            self.assertTrue(parsed.paragraphs)
            self.assertIn("docx_source_inspection_failed", [r["reason"] for r in parsed.source_omissions])

    def test_docx_package_is_loaded_once_and_fallback_regions_are_not_duplicated(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "alternate.docx"
            doc = Document()
            doc.add_paragraph(PROTON + " " + CARBON)
            namespace = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
            alternate = Element(namespace + "AlternateContent")
            for name in ("Choice", "Fallback"):
                wrapper = Element(namespace + name)
                paragraph, run, text = OxmlElement("w:p"), OxmlElement("w:r"), OxmlElement("w:t")
                text.text = CARBON
                run.append(text)
                paragraph.append(run)
                wrapper.append(paragraph)
                alternate.append(wrapper)
            doc.element.body.append(alternate)
            doc.save(path)
            with patch("checksi.file_io.docx.Document", wraps=Document) as load:
                parsed = DocumentParser().parse(str(path), header_margin=0, footer_margin=0)
            self.assertEqual(load.call_count, 1)
            self.assertEqual(len(parsed.source_omissions), 1)

    def test_table_limit_keeps_inspecting_non_table_xml_and_cells_are_not_duplicated(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "limited.docx"
            doc = Document()
            doc.add_paragraph(PROTON + " " + CARBON)
            table = doc.add_table(rows=1, cols=2)
            for cell in table.rows[0].cells:
                cell.text = CARBON
                wrapper, run, text = OxmlElement("w:ins"), OxmlElement("w:r"), OxmlElement("w:t")
                text.text = EA
                run.append(text)
                wrapper.append(run)
                cell.paragraphs[0]._p.append(wrapper)
            paragraph = doc.add_paragraph(CARBON.replace("20.0", "92.0"))
            wrapper = OxmlElement("w:ins")
            paragraph._p.addprevious(wrapper)
            wrapper.append(paragraph._p)
            doc.save(path)
            with patch("checksi.file_io.DOCX_TABLE_CELL_LIMIT", 1):
                parsed = DocumentParser().parse(str(path), header_margin=0, footer_margin=0)
            self.assertEqual(sum(r["reason"] == "docx_table_not_supported" for r in parsed.source_omissions), 1)
            self.assertIn("docx_source_scan_limit", [r["reason"] for r in parsed.source_omissions])
            self.assertTrue(any("92.0" in r["excerpt"] for r in parsed.source_omissions))

    def test_deleted_revision_text_has_its_own_origin_without_repeating_live_text(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "deleted.docx"
            doc = Document()
            paragraph = doc.add_paragraph(PROTON + " " + CARBON)
            wrapper, run, text = OxmlElement("w:del"), OxmlElement("w:r"), OxmlElement("w:delText")
            text.text = CARBON.replace("20.0", "92.0")
            run.append(text)
            wrapper.append(run)
            paragraph._p.append(wrapper)
            doc.save(path)
            parsed = DocumentParser().parse(str(path), header_margin=0, footer_margin=0)
            omission, = parsed.source_omissions
            self.assertEqual(omission["reason"], "docx_deleted_content_not_supported")
            self.assertIn("92.0", omission["excerpt"])
            self.assertNotIn("20.0", omission["excerpt"])

    def test_hyperlink_prose_is_not_repeated_but_split_measurement_is_unassessed(self):
        for extra, expected in ((" supporting reference", False), (" 92.0 (s, 1H)", True)):
            with self.subTest(extra=extra), TemporaryDirectory() as directory:
                path = Path(directory) / "hyperlink.docx"
                doc = Document()
                paragraph = doc.add_paragraph(PROTON + " " + CARBON)
                wrapper, run, text = OxmlElement("w:hyperlink"), OxmlElement("w:r"), OxmlElement("w:t")
                text.text = extra
                run.append(text)
                wrapper.append(run)
                paragraph._p.append(wrapper)
                doc.save(path)
                parsed = DocumentParser().parse(str(path), header_margin=0, footer_margin=0)
                self.assertEqual(bool(parsed.source_omissions), expected)

    def test_wrapped_text_inside_an_assessed_description_is_unassessed(self):
        # Body extraction reads direct runs only; wrapped text without numeric
        # data inside a description must not leave coverage complete.
        for kind in ("ins", "hyperlink"):
            for before, wrapped, after in (("1H NMR (400 MHz, CDCl3): δ 1.00 (", "br s", ", 6H). " + CARBON),
                                           ("1H NMR (400 MHz, ", "CDCl3", "): δ 1.00 (s, 6H). " + CARBON),
                                           ("1H NMR (400 MHz, CDCl3): δ 1.00 (s, 6", "H", "). " + CARBON)):
                with self.subTest(wrapper=kind, text=wrapped), TemporaryDirectory() as directory:
                    path = Path(directory) / "wrapped.docx"
                    doc = Document()
                    paragraph = doc.add_paragraph(before)
                    wrapper, run, text = OxmlElement("w:" + kind), OxmlElement("w:r"), OxmlElement("w:t")
                    text.text = wrapped
                    run.append(text)
                    wrapper.append(run)
                    paragraph._p.append(wrapper)
                    paragraph.add_run(after)
                    doc.save(path)
                    parsed = DocumentParser().parse(str(path), header_margin=0, footer_margin=0)
                    self.assertEqual(parsed.paragraphs, [before + after])
                    omission, = parsed.source_omissions
                    self.assertEqual(omission["reason"], "docx_content_not_supported")
                    self.assertEqual(omission["excerpt"], before + wrapped + after)
                    result = self.analyze(parsed.paragraphs, "disabled", parsed.source_omissions)
                    self.assertEqual(result["assessment"]["status"], "partial")

    def test_deleted_and_table_fallback_content_does_not_duplicate_active_evidence(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "fallback.docx"
            namespace = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
            doc = Document()
            doc.add_paragraph(PROTON + " " + CARBON)
            alternate = Element(namespace + "AlternateContent")
            for name in ("Choice", "Fallback"):
                wrapper = Element(namespace + name)
                deleted, run, text = OxmlElement("w:del"), OxmlElement("w:r"), OxmlElement("w:delText")
                text.text = CARBON.replace("20.0", "92.0")
                run.append(text)
                deleted.append(run)
                wrapper.append(deleted)
                alternate.append(wrapper)
                if name == "Fallback":
                    table = doc.add_table(rows=1, cols=1)
                    table.cell(0, 0).text = CARBON
                    wrapper.append(table._tbl)
            doc.element.body.append(alternate)
            doc.save(path)
            parsed = DocumentParser().parse(str(path), header_margin=0, footer_margin=0)
            omission, = parsed.source_omissions
            self.assertEqual(omission["reason"], "docx_deleted_content_not_supported")


if __name__ == "__main__":
    unittest.main()
