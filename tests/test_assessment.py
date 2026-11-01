"""Coverage accounting with the real controller and isolated run settings."""

import threading
import unittest

from checksi.assessment import build_assessment
from checksi.config_schema import AppConfig
from checksi.controller import CheckSIController
from checksi.events import ProgressEvent


SUPPORTED = "1H NMR (400 MHz, CDCl3): δ 1.00 (s, 6H). 13C NMR (100 MHz, CDCl3): δ 20.0."


class AssessmentTests(unittest.TestCase):
    def setUp(self):
        self.cfg = AppConfig.model_validate({
            "paths": {"input_path": "input.pdf", "output_dir": "."},
            "modes": {"deep_name_analysis": "off", "output_mode": "essential"},
            "solvents": {"entries": []},
            "errors": {"default_severity": "disabled"},
        })

    def analyze(self, paragraphs, **kwargs):
        return CheckSIController(self.cfg, event_dispatcher=lambda _: None).run_analysis(paragraphs, **kwargs)

    def test_supported_grammar_and_zero_recognition_remain_distinct(self):
        for text, expected in [
            (SUPPORTED, "complete"),
            (SUPPORTED.replace("400 MHz", "400MHz"), "none"),
            (SUPPORTED.replace("400 MHz", "400.13 MHz"), "complete"),
            ("General experimental methods.", "none"),
        ]:
            with self.subTest(text=text):
                result = self.analyze([text])
                self.assertEqual(result["assessment"]["status"], expected)
                self.assertEqual(result["total_found"], int(expected == "complete"))
                self.assertTrue(all(count == 0 for count in result["error_stats"].values()))

    def test_orphan_carbon_hrms_and_elemental_data_are_inspectable(self):
        for text in [
            "13C NMR (100 MHz, CDCl3): δ 60.0, 20.0.",
            "HRMS (ESI): calcd for C2H7O+ 47.0491, found 47.0491.",
            "Anal. Calcd for C2H6O: C, 52.14; H, 13.13. Found: C, 52.14; H, 13.13.",
        ]:
            with self.subTest(text=text):
                assessment = self.analyze([text])["assessment"]
                self.assertEqual(assessment["status"], "none")
                omission, = assessment["unassessed"]
                self.assertEqual(omission["excerpt"], text)
                self.assertEqual(omission["reason"], "no_supported_proton_header")
                self.assertEqual(omission["source"]["paragraph_1based"], 1)

    def test_additional_supported_header_in_a_raw_paragraph_is_not_double_counted_or_hidden(self):
        result = self.analyze([SUPPORTED + " " + SUPPORTED])
        self.assertEqual(result["total_found"], 1)
        self.assertEqual(result["assessment"]["status"], "partial")
        omission, = result["assessment"]["unassessed"]
        self.assertEqual(omission["reason"], "additional_description_in_paragraph")

    def test_cancelled_scan_never_claims_complete_coverage(self):
        token = threading.Event()

        def cancel_after_first_scan(event):
            if isinstance(event, ProgressEvent) and event.current == 1:
                token.set()

        result = CheckSIController(self.cfg, event_dispatcher=cancel_after_first_scan).run_analysis(
            [SUPPORTED, SUPPORTED], cancel_token=token)
        self.assertTrue(result["aborted"])
        self.assertEqual(result["total_found"], 1)
        self.assertEqual(result["assessment"]["status"], "partial")
        self.assertIn("scan_incomplete", [item["reason"] for item in result["assessment"]["unassessed"]])

    def test_pre_cancelled_scan_has_no_assessed_descriptions(self):
        token = threading.Event()
        token.set()
        result = self.analyze([SUPPORTED], cancel_token=token)
        self.assertEqual(result["assessment"]["status"], "none")
        self.assertEqual(result["assessment"]["assessed_descriptions"], 0)
        self.assertEqual(result["assessment"]["unassessed"][0]["reason"], "scan_incomplete")

    def test_coverage_snapshot_does_not_share_mutable_sources_between_runs(self):
        source = {"kind": "docx_table", "table_path": [
            {"table_1based": 1, "row_1based": 1, "column_1based": 1},
        ]}
        omissions = [{"source": source, "excerpt": "HRMS", "reason": "docx_table_not_supported"}]
        result = build_assessment(1, omissions)
        source["table_path"][0]["row_1based"] = 99
        omissions.clear()
        self.assertEqual(result["unassessed"][0]["source"]["table_path"][0]["row_1based"], 1)
        self.assertEqual(build_assessment(1)["status"], "complete")
