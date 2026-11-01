"""Review regressions at real report and source-location boundaries."""
from copy import deepcopy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fitz
from lxml import html

from checksi.config_schema import AppConfig
from checksi.report_service import export_reports


def make_config(root):
    return AppConfig.model_validate({
        "paths": {"input_path": str(root / "source.pdf"), "output_dir": str(root / "out")},
        "modes": {"deep_name_analysis": "off", "output_mode": "detailed"},
        "solvents": {"entries": []}, "errors": {"default_severity": "critical", "overrides": {
            "stereo_check_required": "info", "dp5_analysis_failed": "warning"}}})


def make_analysis():
    return {"total_found": 2, "error_stats": {"name_not_found": 2}, "compounds": [
        {"id": i, "source_paragraph_1based": i, "original_text": f"Description {i}. Shared proton signal data. Carbon data {i}.",
         "all_warnings": [f"⚠️ name_not_found. Finding for compound {i}."],
         "highlight_info": {"color": "red" if i == 1 else "yellow", "hnmr_text": "Shared proton signal data", "cnmr_text": f"Carbon data {i}"}}
        for i in (1, 2)]}


class ReviewReportRepairs(unittest.TestCase):
    def test_repeated_phrase_rectangles_have_only_the_owner_at_their_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root)
            data = make_analysis()
            before = deepcopy(data)
            with fitz.open() as source:
                page = source.new_page()
                page.insert_text((50, 80), "Description 1. Shared proton\nsignal data. Carbon data 1.")
                page.insert_text((50, 200), "Description 2. Shared proton\nsignal data. Carbon data 2.")
                source.save(cfg.paths.input_path)
            result = export_reports(cfg, data, pdf_path=str(cfg.paths.input_path), log_callback=lambda _: None)
            self.assertEqual(data, before)
            first = [a for a in result["source_associations"] if a["rect"][1] < 150]
            second = [a for a in result["source_associations"] if a["rect"][1] > 150]
            self.assertEqual({a["compound_id"] for a in first}, {1})
            self.assertEqual({a["compound_id"] for a in second}, {2})
            with fitz.open(result["source_pdf_path"]) as source:
                notes = [a.info["content"] for page in source for a in page.annots() if a.info["title"] != "stat" and a.info["content"]]
            self.assertEqual(len(notes), 2)
            self.assertTrue(all(("compound 1" in n) != ("compound 2" in n) for n in notes))
            payload = json.loads(Path(result["analysis_path"]).read_text(encoding="utf-8"))
            self.assertNotIn("source_associations", payload["export"])

    def test_uncertain_owner_is_explicit_and_does_not_change_artifact_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            cfg=make_config(root)
            data=make_analysis()
            with fitz.open() as source:
                source.new_page().insert_text((50, 80), "Shared proton signal data")
                source.save(cfg.paths.input_path)
            result=export_reports(cfg,data,pdf_path=str(cfg.paths.input_path),log_callback=lambda _:None)
            self.assertFalse(result["partial"])
            self.assertEqual(result["highlighted_compounds"],0)
            self.assertEqual(result["source_associations"],[])
            self.assertTrue(result["annotation_ambiguities"])
            self.assertTrue(result["annotation_warnings"])
            self.assertTrue(Path(result["source_pdf_path"]).is_file())

    def test_uncounted_notes_do_not_acquire_a_counted_severity_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            cfg=make_config(root)
            data=make_analysis()
            data["compounds"][0]["all_warnings"] = ["❗ Free diagnostic", "⚠️ name_not_found. Counted critical", "❗ dp5_analysis_failed. Counted warning", "stereo_check_required. Counted info"]
            result=export_reports(cfg,data,log_callback=lambda _:None)
            doc=html.fromstring(Path(result["html_path"]).read_text(encoding="utf-8"))
            def paragraph(text):
                return doc.xpath('//p[contains(.,"'+text+'") and not(@class="source")]')[0]
            self.assertEqual(paragraph("Free diagnostic").get("class"),"diagnostic")
            self.assertNotIn("Warning:",paragraph("Free diagnostic").text_content())
            for text,severity in (("Counted critical","critical"),("Counted warning","warning"),("Counted info","info")):
                self.assertEqual(paragraph(text).get("class"),severity)
                self.assertTrue(paragraph(text).text_content().startswith(severity.capitalize()+":"))

    def test_nonfinite_data_is_preserved_explicitly_in_strict_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            data=make_analysis()
            data["compounds"][0]["shifts"]=[float("inf"),float("-inf"),float("nan")]
            result=export_reports(make_config(root),data,log_callback=lambda _:None)
            payload=json.loads(Path(result["analysis_path"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["analysis"]["compounds"][0]["shifts"],[None,None,None])
            self.assertEqual([v["value"] for v in payload["nonfinite_numbers"]],["Infinity","-Infinity","NaN"])
            self.assertTrue(math.isinf(data["compounds"][0]["shifts"][0]))
            self.assertTrue(math.isnan(data["compounds"][0]["shifts"][2]))

    def test_metadata_absence_and_required_html_failure_never_leave_complete_json(self):
        import importlib.metadata
        with tempfile.TemporaryDirectory() as tmp:
            cfg=make_config(Path(tmp))
            with patch("checksi.report_service.importlib.metadata.version",side_effect=importlib.metadata.PackageNotFoundError):
                result=export_reports(cfg,make_analysis(),log_callback=lambda _:None)
            payload=json.loads(Path(result["analysis_path"]).read_text(encoding="utf-8"))
            self.assertIsNone(payload["provenance"]["checksi"])
            with patch("checksi.report_service.render_html",side_effect=OSError("required failure")):
                with self.assertRaisesRegex(OSError,"required failure"):
                    export_reports(cfg,make_analysis(),log_callback=lambda _:None)
            payloads=[json.loads(p.read_text(encoding="utf-8")) for p in cfg.paths.output_dir.glob("*/results.json")]
            failed=[p for p in payloads if p["export"]["status"]=="failed"]
            self.assertEqual(len(failed),1)
            self.assertIsNone(failed[0]["export"]["html_path"])

    def test_identical_descriptions_do_not_infer_ownership_from_visual_order(self):
        from checksi.file_io import paragraphs_from_input
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = make_config(root)
            text = '1H NMR (400 MHz, CDCl3): δ 1.0 (s, 3H).'
            with fitz.open() as source:
                page = source.new_page()
                from checksi.report_rendering import font_resources
                page.insert_font(fontname='Fixture', fontbuffer=font_resources()['NotoSans-Regular.ttf'])
                page.insert_text((50, 250), text, fontname='Fixture')
                page.insert_text((50, 100), text, fontname='Fixture')
                source.save(cfg.paths.input_path)
            paragraphs = paragraphs_from_input(str(cfg.paths.input_path), header_margin=0, footer_margin=0)
            self.assertEqual(len(paragraphs), 2)
            data = {'total_found': 2, 'error_stats': {}, 'compounds': [
                {'id': i, 'source_paragraph_1based': i, 'original_text': paragraph,
                 'all_warnings': [f'Compound {i} only'], 'highlight_info': {'hnmr_text': paragraph}}
                for i, paragraph in enumerate(paragraphs, 1)]}
            result = export_reports(cfg, data, pdf_path=str(cfg.paths.input_path), log_callback=lambda _: None)
            self.assertEqual(result['source_associations'], [])
            self.assertEqual(result['highlighted_compounds'], 0)
            self.assertTrue(result['annotation_ambiguities'])
            self.assertFalse(result['partial'])

    def test_optional_failed_file_cleanup_does_not_discard_required_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            original = Path.unlink
            def unlink(path, *args, **kwargs):
                if path.name == 'statistics.xlsx':
                    raise PermissionError('fixture locked incomplete file')
                return original(path, *args, **kwargs)
            with patch('openpyxl.Workbook.save', side_effect=OSError('fixture export failure')), patch.object(Path, 'unlink', unlink):
                result = export_reports(cfg, make_analysis(), log_callback=lambda _: None)
            self.assertTrue(result['partial'])
            self.assertIsNone(result['error_stats_path'])
            self.assertTrue(Path(result['html_path']).is_file())
            self.assertTrue(Path(result['analysis_path']).is_file())
            self.assertIn('locked incomplete file', ' '.join(result['artifact_warnings']))
