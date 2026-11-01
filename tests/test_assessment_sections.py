"""Real-document regressions for unassessed aliases, XML and repeated sections."""

from unittest.mock import patch

from docx import Document
from docx.oxml import OxmlElement

from checksi.config_schema import AppConfig
from checksi.controller import CheckSIController
from checksi.document_parser import DocumentParser
from checksi.analysis.nmr import _extract_nmr_section
from tests.test_assessment_coverage import RealDocumentCase, SUPPORTED, save_pdf


UNSUPPORTED = "1H NMR (400.13 MHz, CDCl3): δ 8.73 (s, 1H)."
ORPHAN_CARBON = "13C NMR (100 MHz, CDCl3): δ 91.1, 81.2."
EA = "Anal calcd for C2H6O: C, 52.14; H, 13.13. Found: C, 52.14; H, 13.13."
MASS = "calcd for C2H7O+ [M+H]+ 47.0491, found 47.0491."


class AssessmentSectionTests(RealDocumentCase):
    def analyze(self, text):
        cfg = AppConfig.model_validate({
            "paths": {"input_path": "input.pdf", "output_dir": "."},
            "modes": {"deep_name_analysis": "off", "output_mode": "essential"},
            "solvents": {"entries": []}, "errors": {"default_severity": "disabled"},
        })
        return CheckSIController(cfg, event_dispatcher=lambda _: None).run_analysis([text])

    def test_orphan_and_table_aliases_retain_evidence_with_disabled_findings(self):
        aliases = {name: f"{name}: {MASS}" for name in ("APCI", "ESI", "MALDI", "TOF", "HRESIMS", "HRMS")}
        aliases["dotless-EA"] = EA
        for name, text in aliases.items():
            for extension in ("pdf", "docx"):
                with self.subTest(alias=name, format=extension):
                    source = self.root / f"alias-{name}.{extension}"
                    if extension == "pdf":
                        save_pdf(source, text + " " + SUPPORTED)
                    else:
                        document = Document()
                        document.add_paragraph(SUPPORTED)
                        document.add_table(rows=1, cols=1).cell(0, 0).text = text
                        document.save(source)
                    _, assessment = self.assert_incomplete_outcome(source, assessed=1, status="partial")
                    omission, = assessment["unassessed"]
                    self.assertIn(text, omission["excerpt"])

    def test_ordinary_prose_and_one_hrms_header_with_aliases_do_not_create_extra_candidates(self):
        for text in [
            "Yellow resin, toffee-like texture. " + SUPPORTED,
            "Mesityl derivative in stoff and resin. " + SUPPORTED,
            "ESI and TOF were used for the instrument setup. " + SUPPORTED,
            SUPPORTED + " HRMS (ESI-TOF): " + MASS,
        ]:
            with self.subTest(text=text):
                result = self.analyze(text)
                self.assertEqual(result["assessment"]["status"], "complete")
                self.assertEqual(result["assessment"]["unassessed"], [])

    def test_wrapped_body_and_cell_paragraphs_and_hyperlinks_are_unassessed(self):
        for region, kind in [("body", "ins"), ("body", "sdt"), ("table", "ins"),
                             ("table", "sdt"), ("body", "hyperlink")]:
            with self.subTest(region=region, wrapper=kind):
                source = self.root / f"wrapped-{region}-{kind}.docx"
                document = Document()
                document.add_paragraph(SUPPORTED)
                paragraph = (document.add_paragraph(UNSUPPORTED) if region == "body"
                             else document.add_table(rows=1, cols=1).cell(0, 0).paragraphs[0])
                paragraph.text = UNSUPPORTED
                if kind == "hyperlink":
                    paragraph.clear()
                    wrapper = OxmlElement("w:hyperlink")
                    run, text = OxmlElement("w:r"), OxmlElement("w:t")
                    text.text = UNSUPPORTED
                    run.append(text)
                    wrapper.append(run)
                    paragraph._p.append(wrapper)
                else:
                    wrapper = OxmlElement("w:" + kind)
                    paragraph._p.addprevious(wrapper)
                    inner = OxmlElement("w:sdtContent") if kind == "sdt" else wrapper
                    if inner is not wrapper:
                        wrapper.append(inner)
                    inner.append(paragraph._p)
                document.save(source)
                payload, assessment = self.assert_incomplete_outcome(source, assessed=1, status="partial")
                self.assertNotIn("400.13", payload["analysis"]["compounds"][0]["original_text"])
                self.assertTrue(any("400.13" in item["excerpt"] for item in assessment["unassessed"]))
                self.assertTrue(all(item["source"]["kind"] in ("document", "docx_table")
                                    for item in assessment["unassessed"]))

    def test_second_carbon_section_keeps_its_own_span_and_first_assignment(self):
        text = SUPPORTED + " Another compound. " + ORPHAN_CARBON
        for extension in ("pdf", "docx"):
            with self.subTest(format=extension):
                source = self.root / f"second-carbon.{extension}"
                if extension == "pdf":
                    save_pdf(source, text)
                else:
                    document = Document()
                    document.add_paragraph(text)
                    document.save(source)
                parsed = DocumentParser().parse(str(source), header_margin=0, footer_margin=0)
                self.assertEqual(len(parsed.paragraphs), 1)
                payload, assessment = self.assert_incomplete_outcome(source, assessed=1, status="partial")
                compound, = payload["analysis"]["compounds"]
                self.assertIn("60.0, 20.0", compound["highlight_info"]["cnmr_text"])
                self.assertNotIn("91.1", compound["highlight_info"]["cnmr_text"])
                omission, = assessment["unassessed"]
                location = omission["source"]
                selected = compound["original_text"][location["start_offset"]:location["end_offset"]]
                self.assertEqual(selected.strip(), ORPHAN_CARBON)

    def test_repeated_mass_and_ea_blocks_are_explicit_without_new_compounds(self):
        for block in (EA, "HRMS (ESI): " + MASS):
            with self.subTest(block=block):
                result = self.analyze(SUPPORTED + " " + block + " " + block)
                self.assertEqual(result["total_found"], 1)
                self.assertEqual(result["assessment"]["status"], "partial")
                omission, = result["assessment"]["unassessed"]
                self.assertEqual(omission["reason"], "additional_analysis_section")

    def test_unknown_xml_region_budget_cannot_turn_remaining_content_into_complete_coverage(self):
        source = self.root / "limited-unknown-regions.docx"
        document = Document()
        document.add_paragraph(SUPPORTED)
        for _ in range(2):
            paragraph = document.add_paragraph(UNSUPPORTED)
            wrapper = OxmlElement("w:ins")
            paragraph._p.addprevious(wrapper)
            wrapper.append(paragraph._p)
        document.save(source)
        with patch("checksi.file_io.DOCX_SOURCE_REGION_LIMIT", 1):
            _, assessment = self.assert_incomplete_outcome(source, assessed=1, status="partial")
        self.assertIn("docx_source_scan_limit", [item["reason"] for item in assessment["unassessed"]])

    def test_prefixed_and_hyphenated_instrument_aliases_are_candidates(self):
        for alias in ("HRESI-MS", "HRESI", "HRAPCI-MS"):
            for extension in ("pdf", "docx"):
                with self.subTest(alias=alias, format=extension):
                    source = self.root / f"prefixed-{alias}.{extension}"
                    orphan = alias + ": " + MASS
                    if extension == "pdf":
                        save_pdf(source, orphan + " " + SUPPORTED)
                    else:
                        document = Document()
                        document.add_paragraph(SUPPORTED)
                        document.add_table(rows=1, cols=1).cell(0, 0).text = orphan
                        document.save(source)
                    _, assessment = self.assert_incomplete_outcome(source, assessed=1, status="partial")
                    omission, = assessment["unassessed"]
                    self.assertIn(alias, omission["excerpt"])

    def test_carbon_coverage_uses_the_existing_decoupling_priority(self):
        proton = SUPPORTED.split(" 13C", 1)[0]
        for first, second, expected, omitted in [
            ("13C NMR (100 MHz, CDCl3): δ 60.0, 20.0.",
             "13C{1H} NMR (100 MHz, CDCl3): δ 91.1, 81.2.", "91.1, 81.2", "60.0, 20.0"),
            ("13C{19F} NMR (100 MHz, CDCl3): δ 91.1, 81.2.",
             "13C NMR (100 MHz, CDCl3): δ 60.0, 20.0.", "60.0, 20.0", "91.1, 81.2"),
        ]:
            for extension in ("pdf", "docx"):
                with self.subTest(first=first, format=extension):
                    text = proton + " " + first + " Another compound. " + second
                    source = self.root / f"priority-{extension}.{extension}"
                    if extension == "pdf":
                        save_pdf(source, text)
                    else:
                        document = Document()
                        document.add_paragraph(text)
                        document.save(source)
                    payload, assessment = self.assert_incomplete_outcome(source, assessed=1, status="partial")
                    compound, = payload["analysis"]["compounds"]
                    self.assertIn(expected, compound["highlight_info"]["cnmr_text"])
                    omission, = assessment["unassessed"]
                    start, end = omission["source"]["start_offset"], omission["source"]["end_offset"]
                    self.assertIn(omitted, compound["original_text"][start:end])
                    self.assertNotIn(expected, compound["original_text"][start:end])

    def test_selected_carbon_before_proton_is_not_reported_as_unassessed(self):
        proton = SUPPORTED.split(" 13C", 1)[0]
        selected = "13C{1H} NMR (100 MHz, CDCl3): δ 91.1, 81.2."
        result = self.analyze(selected + " " + proton)
        self.assertIn("91.1, 81.2", result["compounds"][0]["highlight_info"]["cnmr_text"])
        self.assertEqual(result["assessment"]["status"], "complete")
        text = selected + " " + proton + " " + ORPHAN_CARBON
        result = self.analyze(text)
        omission, = result["assessment"]["unassessed"]
        location = omission["source"]
        self.assertEqual(text[location["start_offset"]:location["end_offset"]].strip(), ORPHAN_CARBON)

    def test_all_carbon_pattern_priorities_and_flags_remain_characterized(self):
        headers = [("13C{1H}", 0), ("13C", 0), ("13C{1H, 19F}", 1),
                   ("13C{19F, 1H}", 1), ("13C{19F}", 1)]
        for priority, (preferred, flag) in enumerate(headers):
            preferred_text = f"{preferred} NMR (100 MHz, CDCl3): δ 91.1, 81.2."
            section, selected_flag = _extract_nmr_section(preferred_text, "13C")
            self.assertIn("91.1, 81.2", section)
            self.assertEqual(selected_flag, flag)
            for later, _ in headers[priority + 1:]:
                with self.subTest(preferred=preferred, later=later):
                    later_text = f"{later} NMR (100 MHz, CDCl3): δ 60.0, 20.0."
                    section, selected_flag = _extract_nmr_section(later_text + " " + preferred_text, "13C")
                    self.assertIn("91.1, 81.2", section)
                    self.assertEqual(selected_flag, flag)
