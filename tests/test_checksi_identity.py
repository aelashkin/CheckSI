"""Exercise renamed identity at the real analysis-to-report boundary."""

from copy import deepcopy
import importlib.metadata
import json
from pathlib import Path
import tempfile
import unittest
import unicodedata

import fitz

from checksi.config import ConfigManager
from checksi.controller import CheckSIController
from checksi.report_service import export_reports


class CheckSIIdentityTests(unittest.TestCase):
    def test_refused_formula_keeps_evidence_and_v2_identity_in_real_reports(self):
        text = (
            "1H NMR (400 MHz, CDCl3): δ 1.00 (s, 6H). "
            "13C NMR (100 MHz, CDCl3): δ 20.0. "
            "Anal. Calcd for C2H6O(H2O): C, 52.14; H, 13.13. "
            "Found: C, 52.14; H, 13.13."
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = ConfigManager.load_factory()
            cfg.paths.input_path = root / "source.pdf"
            cfg.paths.output_dir = root / "reports"
            with fitz.open() as document:
                document.new_page().insert_text((50, 50), "Synthetic formula source.")
                document.save(cfg.paths.input_path)
            result = CheckSIController(cfg, event_dispatcher=lambda _: None).run_analysis([text])
            self.assertEqual(result["assessment"]["status"], "partial")
            self.assertTrue(any("Formula not checked by CheckSI:" in note
                                for note in result["compounds"][0]["all_warnings"]))
            original = deepcopy(result)
            output = export_reports(cfg, result, log_callback=lambda _: None)
            payload = json.loads(Path(output["analysis_path"]).read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["provenance"]["checksi"], importlib.metadata.version("checksi"))
            self.assertNotIn("data" + "_checker", payload["provenance"])
            self.assertEqual(payload["analysis"], original)
            self.assertEqual(result, original)
            markup = Path(output["html_path"]).read_text(encoding="utf-8")
            self.assertIn("<h1>CheckSI findings</h1>", markup)
            self.assertIn("Formula source evidence", markup)
            self.assertIn("C2H6O(H2O)", markup)
            with fitz.open(output["pdf_path"]) as document:
                rendered = unicodedata.normalize("NFKC", "".join(page.get_text() for page in document))
            self.assertIn("CheckSI findings", rendered)
            self.assertIn("Formula source evidence", rendered)


if __name__ == "__main__":
    unittest.main()
