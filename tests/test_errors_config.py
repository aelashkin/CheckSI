"""Saved severity compatibility and real finding/counter behavior."""

import tempfile
import unittest
from pathlib import Path

from checksi.analysis.formula_utils import validate_formula_and_get_mass
from checksi.analysis.nmr import check_nmr
from checksi.config import ConfigManager, ConfigValidationError
from checksi.config_schema import AppConfig
from checksi.errors import ErrorCode, emit_error
from checksi.errors.registry import ERROR_TEMPLATES
from checksi.utils.error_handler import ErrorTracker


def make_config(overrides=None):
    """Build a deliberately explicit in-memory configuration."""
    return AppConfig.model_validate({
        "paths": {"input_path": "input.pdf", "output_dir": "report.docx"},
        "modes": {"deep_name_analysis": "off", "output_mode": "essential"},
        "solvents": {"entries": [{"name": "CDCl3", "aliases": [], "proton_shifts": [7.26], "carbon_shifts": [77.0]}]},
        "errors": {"default_severity": "critical", "overrides": overrides or {}},
    })


class ErrorConfigurationTests(unittest.TestCase):
    """Exercise persisted keys through their real emitting paths."""

    def test_legacy_keys_load_save_and_preserve_severities(self):
        overrides = {
            "nmr_solvent_or_frequency_missing": "disabled",
            "nmr_solvent_and_frequency_missing": "critical",
            "formula_atom_symbol_invalid": "info",
            "formula_invalid_format": "critical",
            "hrms_formula_missing": "warning",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.toml"
            ConfigManager(path).save(make_config())
            with path.open("a", encoding="utf-8") as handle:
                for key, severity in overrides.items():
                    handle.write(f'{key} = "{severity}"\n')
            manager = ConfigManager(path)
            loaded = manager.load()
            canonical_overrides = {key: value for key, value in overrides.items() if key != "nmr_solvent_and_frequency_missing"}
            canonical_overrides.update(nmr_configuration_dependent='warning', dp5_configuration_dependent='warning')
            self.assertEqual({key.value: value.value for key, value in loaded.errors.overrides.items()}, canonical_overrides)
            tracker = ErrorTracker()
            findings = check_nmr(loaded, tracker, "13C NMR δ 60.0.", "13C", 1, None, 0, 0, 0, None, 0, 0)[0]
            self.assertFalse(any("nmr_solvent_or_frequency_missing." in line for line in findings))
            _, atom_finding, _ = validate_formula_and_get_mass(loaded, tracker, "Cx2H6")
            self.assertTrue(atom_finding.startswith("formula_atom_symbol_invalid."))
            self.assertEqual(tracker.get_count("nmr_solvent_or_frequency_missing"), 0)
            self.assertEqual(tracker.get_count("formula_atom_symbol_invalid"), 1)
            manager.save(loaded)
            self.assertEqual(manager.load(), loaded)
            for key in canonical_overrides:
                self.assertIn(key, path.read_text(encoding="utf-8"))
            self.assertNotIn("nmr_solvent_and_frequency_missing", path.read_text(encoding="utf-8"))

    def test_legacy_nmr_override_controls_actual_finding_and_disabled_count(self):
        for severity, prefix, expected_count in [("info", "nmr_", 1), ("warning", "❗ ", 1), ("disabled", None, 0)]:
            with self.subTest(severity=severity):
                cfg = make_config({"nmr_solvent_or_frequency_missing": severity})
                tracker = ErrorTracker()
                findings = check_nmr(cfg, tracker, "13C NMR δ 60.0, 20.0.", "13C", 1, None, 0, 0, 0, None, 0, 0)[0]
                matching = [line for line in findings if "nmr_solvent_or_frequency_missing." in line]
                self.assertEqual(len(matching), expected_count)
                if prefix:
                    self.assertTrue(matching[0].startswith(prefix), matching)
                self.assertEqual(tracker.get_count("nmr_solvent_or_frequency_missing"), expected_count)

    def test_candidate_nmr_key_is_fallback_and_legacy_key_wins_conflict(self):
        for overrides, expected in [
            ({"nmr_solvent_and_frequency_missing": "warning"}, "❗ "),
            ({"nmr_solvent_and_frequency_missing": "disabled"}, None),
            ({"nmr_solvent_and_frequency_missing": "critical", "nmr_solvent_or_frequency_missing": "info"}, "nmr_"),
            ({"nmr_solvent_and_frequency_missing": "critical", "nmr_solvent_or_frequency_missing": "disabled"}, None),
        ]:
            with self.subTest(overrides=overrides):
                cfg = make_config(overrides)
                tracker = ErrorTracker()
                findings = check_nmr(cfg, tracker, "13C NMR δ 60.0.", "13C", 1, None, 0, 0, 0, None, 0, 0)[0]
                matching = [line for line in findings if "nmr_solvent_or_frequency_missing." in line]
                if expected:
                    self.assertEqual(len(matching), 1)
                    self.assertTrue(matching[0].startswith(expected))
                else:
                    self.assertEqual(matching, [])
                self.assertEqual(tracker.get_count("nmr_solvent_or_frequency_missing"), int(bool(expected)))

    def test_atom_override_remains_distinct_from_general_formula_format(self):
        for severity, expected in [("info", "formula_atom_symbol_invalid."), ("warning", "❗ "), ("disabled", None)]:
            with self.subTest(severity=severity):
                cfg = make_config({"formula_atom_symbol_invalid": severity, "formula_invalid_format": "critical"})
                tracker = ErrorTracker()
                mass, finding, _ = validate_formula_and_get_mass(cfg, tracker, "Cx2H6")
                self.assertIsNone(mass)
                if expected:
                    self.assertTrue(finding.startswith(expected), finding)
                else:
                    self.assertIsNone(finding)
                self.assertEqual(tracker.get_count("formula_atom_symbol_invalid"), int(bool(expected)))
                self.assertEqual(tracker.get_count("formula_invalid_format"), 0)

    def test_retired_hrms_key_does_not_toggle_other_findings(self):
        cfg = make_config({"hrms_formula_missing": "disabled"})
        tracker = ErrorTracker()
        finding = emit_error(cfg, tracker, ErrorCode.FORMULA_NOT_FOUND)
        self.assertTrue(finding.startswith("⚠️ "))
        self.assertEqual(tracker.get_count("formula_not_found"), 1)
        self.assertEqual(tracker.get_count("hrms_formula_missing"), 0)

    def test_registry_and_independent_counters_include_stable_keys(self):
        for key in ["nmr_solvent_or_frequency_missing", "formula_atom_symbol_invalid", "hrms_formula_missing"]:
            self.assertIn(ErrorCode(key), ERROR_TEMPLATES)
        first, second = ErrorTracker(), ErrorTracker()
        emit_error(make_config(), first, ErrorCode.NMR_SIGNAL_FORMAT, {"error": "bad signal"})
        self.assertEqual(first.get_count("nmr_signal_format"), 1)
        self.assertEqual(second.get_count("nmr_signal_format"), 0)
        copied = first.to_dict()
        copied["nmr_signal_format"] = 10
        self.assertEqual(first.get_count("nmr_signal_format"), 1)

    def test_unknown_saved_error_key_still_fails_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.toml"
            ConfigManager(path).save(make_config())
            with path.open("a", encoding="utf-8") as handle:
                handle.write('not_a_real_error = "warning"\n')
            with self.assertRaises(ConfigValidationError) as error:
                ConfigManager(path).load()
            self.assertIn("not_a_real_error", str(error.exception.__cause__))
