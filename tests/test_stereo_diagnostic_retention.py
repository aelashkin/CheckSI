"""Retain stereo diagnostics without treating homogeneity as validity."""

import unittest
from unittest.mock import patch

from checksi.analysis.stereochem import check_all_same_type, check_streo_desc
from checksi.config_schema import AppConfig
from checksi.controller import CheckSIController
from checksi.errors import ErrorCode
from checksi.utils.error_handler import ErrorTracker


def config(severity):
    return AppConfig.model_validate({
        "paths": {"input_path": "input.pdf", "output_dir": "."},
        "modes": {"deep_name_analysis": "off", "output_mode": "essential"},
        "solvents": {"entries": []},
        "errors": {"default_severity": "disabled", "overrides": {
            ErrorCode.STEREO_DESCRIPTOR_INVALID: severity,
            ErrorCode.STEREO_DESCRIPTOR_INCONSISTENT: severity,
        }},
    })


def messages(value):
    if not value:
        return []
    return [value] if isinstance(value, str) else list(value)


class StereoDiagnosticRetentionTests(unittest.TestCase):
    def test_homogeneous_invalid_grouping_keeps_classification_and_diagnostic(self):
        for severity in ("disabled", "info", "warning", "critical"):
            for descriptor, expected_type in (("(R,S)", 4), ("(1R)", 1)):
                with self.subTest(severity=severity, descriptor=descriptor):
                    tracker = ErrorTracker()
                    homogeneous, type_id, output = check_all_same_type(config(severity), tracker, [descriptor])
                    self.assertTrue(homogeneous)
                    self.assertEqual(type_id, expected_type)
                    expected = int(severity != "disabled")
                    self.assertEqual(tracker.get_count("stereo_descriptor_invalid"), expected)
                    self.assertEqual(len(messages(output)), expected)
                    if expected:
                        prefix = {"info": "stereo_descriptor_invalid.", "warning": "❗ ", "critical": "⚠️ "}[severity]
                        self.assertTrue(messages(output)[0].startswith(prefix))

    def test_later_unrecognized_token_keeps_every_previously_emitted_diagnostic(self):
        for severity in ("disabled", "info", "warning", "critical"):
            with self.subTest(severity=severity):
                tracker = ErrorTracker()
                homogeneous, type_id, output = check_all_same_type(
                    config(severity), tracker, ["(R,S)", "(bogus)"],
                )
                self.assertFalse(homogeneous)
                self.assertIsNone(type_id)
                expected = 0 if severity == "disabled" else 2
                self.assertEqual(tracker.get_count("stereo_descriptor_invalid"), expected)
                self.assertEqual(len(messages(output)), expected)
                if expected:
                    self.assertIn("invalid stereodescription type", messages(output)[0])
                    self.assertIn("unrecognized stereodescriptor", messages(output)[1])

    def test_first_unrecognized_token_preserves_the_string_adapter(self):
        for severity in ("disabled", "critical"):
            with self.subTest(severity=severity):
                tracker = ErrorTracker()
                homogeneous, type_id, output = check_all_same_type(config(severity), tracker, ["(bogus)"])
                self.assertFalse(homogeneous)
                self.assertIsNone(type_id)
                self.assertIsInstance(output, str)
                self.assertEqual(bool(output), severity != "disabled")

    def test_valid_homogeneous_tokens_keep_the_existing_empty_output(self):
        for descriptors, expected_type in ((["(R)"], 4), ([None, "(1R,2S)"], 1), (["(R*)"], 6)):
            with self.subTest(descriptors=descriptors):
                tracker = ErrorTracker()
                self.assertEqual(check_all_same_type(config("critical"), tracker, descriptors),
                                 (True, expected_type, None))
                self.assertEqual(sum(tracker.to_dict().values()), 0)

    def test_mixed_valid_types_keep_the_existing_mixed_classification(self):
        tracker = ErrorTracker()
        homogeneous, type_id, output = check_all_same_type(config("critical"), tracker, ["(R)", "(1R,2S)"])
        self.assertFalse(homogeneous)
        self.assertIsNone(type_id)
        self.assertEqual(tracker.get_count("stereo_descriptor_inconsistent"), 1)
        self.assertEqual(tracker.get_count("stereo_descriptor_invalid"), 0)
        self.assertEqual(len(messages(output)), 1)

    def test_real_stereo_caller_keeps_grouping_diagnostic_with_its_counter(self):
        for severity in ("disabled", "info", "warning", "critical"):
            with self.subTest(severity=severity):
                tracker = ErrorTracker()
                output = check_streo_desc(config(severity), tracker, "(R,S)-example", 100, 0, 0)
                expected = int(severity != "disabled")
                self.assertEqual(tracker.get_count("stereo_descriptor_invalid"), expected)
                self.assertEqual(len([line for line in output if "stereo_descriptor_invalid." in line]), expected)

    def test_controller_preserves_the_same_messages_and_counts(self):
        description = (
            "1H NMR (400 MHz, CDCl3): δ 3.50 (s, 2H), 1.20 (s, 6H). "
            "13C NMR (100 MHz, CDCl3): δ 60.0, 20.0."
        )
        for severity in ("disabled", "info", "warning", "critical"):
            with self.subTest(severity=severity):
                # Replace only the external name resolver; stereo and controller are real.
                with patch("checksi.controller._smiles_and_name_from_paragraph",
                           return_value=(None, "(R,S)-example", "1", {}, [])):
                    result = CheckSIController(config(severity)).run_analysis(["Name (1)", description])
                compound, = result["compounds"]
                expected = int(severity != "disabled")
                self.assertEqual(result["error_stats"]["stereo_descriptor_invalid"], expected)
                self.assertEqual(sum(result["error_stats"].values()), expected)
                self.assertEqual(len([line for line in compound["all_warnings"]
                                      if "stereo_descriptor_invalid." in line]), expected)
