"""Offscreen assessment summaries independent of findings and artifact status."""

import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel

from checksi.application_runner import ApplicationRunResult
from checksi.gui.main_window import DashboardPage


class AssessmentDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.page = DashboardPage()
        self.page.resize(1100, 900)
        self.page.show()
        self.addCleanup(self.page.close)

    def display(self, status, *, partial_export=False, aborted=False, report_available=True):
        assessment = {
            "status": status, "assessed_descriptions": 0 if status == "none" else 1,
            "unassessed": [] if status == "complete" else [{
                "source": {"kind": "paragraph", "paragraph_1based": 2},
                "excerpt": '<img src="untrusted">', "reason": "unsupported_proton_header",
            }],
        }
        html = self.root / "findings.html"
        html.write_text("<p>Assessment details</p>")
        result = ApplicationRunResult(
            input_path="sample.pdf",
            analysis_data={"total_found": assessment["assessed_descriptions"],
                           "assessment": assessment, "aborted": aborted},
            report_data={"partial": partial_export, "html_path": str(html) if report_available else None,
                         "artifact_warnings": ["Optional PDF unavailable"] if partial_export else []},
            issue_counts={"critical": 0, "warning": 0, "info": 0, "anomaly_count": 0},
        )
        self.page.show_summary(result, str(self.root))
        self.app.processEvents()
        text = "\n".join(label.text() for label in self.page.findChildren(QLabel) if label.isVisible())
        return result, text

    def test_nothing_assessed_is_visible_with_zero_findings_and_complete_export(self):
        _, text = self.display("none")
        self.assertIn("Nothing assessed", text)
        self.assertIn("0 descriptions assessed", text)
        self.assertIn("1 unassessed content record", text)
        self.assertNotIn("Partial export", text)
        self.assertTrue(self.page.open_html_button.isEnabled())

    def test_partial_assessment_and_partial_export_are_both_visible(self):
        _, text = self.display("partial", partial_export=True)
        self.assertIn("Partly assessed", text)
        self.assertIn("Partial export", text)
        self.assertNotIn("%", self.page.assessment_counts.text())

    def test_complete_assessment_does_not_claim_clean_chemistry(self):
        _, text = self.display("complete")
        self.assertIn("Assessment: Assessed", text)
        self.assertIn("1 description assessed", text)
        self.assertNotIn("Partly assessed", text)
        self.assertNotIn("Nothing assessed", text)
        self.assertNotIn("clean", text.lower())

    def test_cancellation_and_untrusted_excerpt_do_not_replace_assessment_or_export_state(self):
        _, text = self.display("none", partial_export=True, aborted=True)
        self.assertIn("Nothing assessed", text)
        self.assertIn("Cancelled", text)
        self.assertEqual(self.page.assessment_counts.textFormat(), Qt.TextFormat.PlainText)

    def test_cancelled_run_without_reports_does_not_offer_findings_details(self):
        self.display("none", aborted=True, report_available=False)
        self.assertNotIn("Open Findings", self.page.assessment_counts.text())
        self.assertFalse(self.page.open_html_button.isEnabled())

    def test_legacy_results_and_clear_hide_previous_assessment(self):
        result, _ = self.display("partial")
        result.analysis_data.pop("assessment")
        self.page.show_summary(result, str(self.root))
        self.assertTrue(self.page.assessment_badge.isHidden())
        self.assertTrue(self.page.assessment_counts.isHidden())
        self.page.clear_summary()
        self.assertTrue(self.page.summary_card.isHidden())
