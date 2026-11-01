"""Long refused tokens remain readable without changing analytical evidence."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import fitz
from lxml import html

from checksi.config_schema import AppConfig
from checksi.report_service import export_reports
from checksi.report_rendering import _formula_html, render_html, render_pdf
from checksi.controller import CheckSIController
from checksi.utils.error_handler import ErrorTracker
from tests.test_hrms import make_config


class FormulaReportWrappingTests(unittest.TestCase):
    def test_long_source_tokens_keep_all_pdf_columns_and_exact_semantic_values(self):
        for token in ("C2H6O<script>literal</script>" + "X" * 180,
                      "C2H6O" + "+" * 180 + "opaque"):
            with self.subTest(token=token[:30]), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "input.pdf"
                with fitz.open() as document:
                    document.new_page().insert_text((50, 80), "Synthetic source.")
                    document.save(source)
                cfg = AppConfig.model_validate({
                    "paths": {"input_path": str(source), "output_dir": str(root / "out")},
                    "modes": {"deep_name_analysis": "off", "output_mode": "essential"},
                    "solvents": {"entries": []}, "errors": {"default_severity": "warning"},
                })
                analysis = {"total_found": 1, "error_stats": {}, "compounds": [{
                    "id": 1, "original_text": token, "all_warnings": [
                        "formula_source_review_required. Formula-source comparability requires scientific review."],
                    "highlight_info": {"color": "blue"},
                    "formula_sources": {"ea": {
                        "candidate": {"raw": token}, "formula": None, "status": "unsupported",
                        "composition": {"state": "unknown", "reason": "Complete source token is unsupported."},
                    }},
                }]}
                before = deepcopy(analysis)
                exported = export_reports(cfg, analysis, log_callback=lambda _: None)
                self.assertFalse(exported["partial"])
                markup = Path(exported["html_path"]).read_text()
                tree = html.fromstring(markup)
                self.assertEqual(tree.xpath('//p[@class="source"]')[0].text_content(), token)
                raw_cell = tree.xpath('//h3[text()="Formula source evidence"]/following-sibling::table[1]//tr[2]/td[2]')[0]
                self.assertEqual(raw_cell.text_content(), token)
                self.assertFalse(tree.xpath('//script'))
                with fitz.open(exported["pdf_path"]) as document:
                    words = [word for page in document for word in page.get_text(
                        "words", clip=fitz.Rect(-10000, -10000, 10000, 10000))]
                    self.assertTrue(words)
                    self.assertGreaterEqual(min(word[0] for word in words), 0)
                    self.assertLessEqual(max(word[2] for word in words), document[0].rect.width)
                    # Raw evidence must fit its cell, not merely the page.
                    parsed_left = None
                    table_page = None
                    table_top = None
                    for page_index, page in enumerate(document):
                        headings = page.search_for("Parsed")
                        if headings:
                            parsed_left = headings[-1].x0
                            table_page = page_index
                            table_top = headings[-1].y1
                    self.assertIsNotNone(parsed_left)
                    evidence_words = []
                    for page_index in range(table_page, len(document)):
                        page = document[page_index]
                        page_words = page.get_text("words")
                        next_rows = [word[1] for word in page_words if word[4] == "HRMS"]
                        bottom = min(next_rows) if next_rows else page.rect.height
                        top = table_top if page_index == table_page else 0
                        evidence_words.extend(word for word in page_words
                                              if top <= word[1] < bottom
                                              and ("XXXXX" in word[4] or "+++++" in word[4]))
                        if next_rows:
                            break
                    self.assertTrue(evidence_words)
                    self.assertTrue(all(word[2] <= parsed_left + 1 for word in evidence_words),
                                    "Raw formula text overlaps the parsed-composition column")
                    rendered = " ".join(word[4] for word in words)
                    for heading in ("Parsed", "composition", "status", "Basis"):
                        self.assertIn(heading, rendered)
                saved = json.loads(Path(exported["analysis_path"]).read_text())
                self.assertEqual(saved["analysis"], before)
                self.assertEqual(analysis, before)


class FormulaReportFidelityTests(unittest.TestCase):
    def test_empty_formula_evidence_is_not_rendered(self):
        result = CheckSIController(make_config())._analyze_single_description('', None, ErrorTracker())
        self.assertEqual(_formula_html(result), '')

    def test_pdf_source_text_retains_long_chemical_tokens(self):
        name = "N,N'-(((1R,3S)-cyclopentane-1,3-diyl)bis(methylene))dibenzamide"
        formula = 'C123H234N56O78Si12'
        analysis = {'error_stats': {}, 'compounds': [{'id': 1, 'name': name, 'original_text': name + ' ' + formula, 'all_warnings': []}]}
        markup, resources = render_html(make_config(), analysis, '', {'artifact_warnings': []}, {'name': 'test.pdf'})
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / 'findings.pdf'
            render_pdf(markup, resources, destination)
            with fitz.open(destination) as document:
                extracted = ''.join(page.get_text() for page in document)
        self.assertIn(name, extracted)
        self.assertIn(formula, extracted)
