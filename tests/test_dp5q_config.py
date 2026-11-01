"""DP5q settings migrate in memory and save only the owned runtime fields."""

from __future__ import annotations

import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from checksi.config import ConfigManager, ConfigValidationError
from checksi.config_schema import DP5Settings


CONFIG = '''# user configuration
[paths]
input_path = "input.pdf" # keep this path comment
output_dir = "."
[modes]
deep_name_analysis = "off"
output_mode = "essential"
[solvents]
entries = [{ name = "CDCl3", aliases = [], proton_shifts = [7.28], carbon_shifts = [77.0] }]
[errors]
default_severity = "warning"
[dp5]
enabled = false
'''


class DP5qConfigTests(unittest.TestCase):
    """Exercise migration at the schema and actual TOML persistence boundaries."""

    def test_threshold_alias_load_has_a_migration_notice(self):
        with self.assertWarnsRegex(UserWarning, "score_threshold_percent"):
            DP5Settings.model_validate({"probability_threshold_percent": 20})

    def test_defaults_and_serialization_have_only_active_fields(self) -> None:
        self.assertEqual(DP5Settings().model_dump(), {
            "enabled": False, "score_threshold_percent": 20.0,
            "timeout_seconds": 600, "keep_artifacts": False,
        })

    def test_factory_and_example_thresholds_are_twenty_and_saved_values_are_preserved(self):
        self.assertEqual(ConfigManager.load_factory().dp5.score_threshold_percent, 20.)
        example = Path(__file__).resolve().parents[1] / 'settings.toml'
        self.assertEqual(ConfigManager(example).load().dp5.score_threshold_percent, 20.)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.toml'
            path.write_text(CONFIG + 'score_threshold_percent = 10.0\n', encoding='utf-8')
            before = path.read_bytes()
            self.assertEqual(ConfigManager(path).load().dp5.score_threshold_percent, 10.)
            self.assertEqual(path.read_bytes(), before)

    def test_old_threshold_loads_and_equal_spellings_are_accepted(self) -> None:
        for values in [
            {"probability_threshold_percent": 22.5},
            {"probability_threshold_percent": 22.5, "score_threshold_percent": 22.5},
        ]:
            with self.subTest(values=values):
                settings = DP5Settings.model_validate(values)
                self.assertEqual(settings.score_threshold_percent, 22.5)
                self.assertNotIn("probability_threshold_percent", settings.model_dump())

    def test_conflicting_spellings_and_unknown_keys_fail(self) -> None:
        for values in [
            {"probability_threshold_percent": 20, "score_threshold_percent": 21},
            {"runtime": "somewhere"},
            {"score_threshold_percent": -1},
            {"score_threshold_percent": 101},
        ]:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                DP5Settings.model_validate(values)

    def test_legacy_load_does_not_write_and_save_preserves_unrelated_comments(self) -> None:
        legacy = CONFIG + '''managed = true
install_root = "/unused/reference"
expected_rev = "obsolete"
probability_threshold_percent = 17.5 # advisory threshold
timeout_seconds = 30
keep_artifacts = true
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            path.write_text(legacy, encoding="utf-8")
            original_bytes = path.read_bytes()
            manager = ConfigManager(path)
            with warnings.catch_warnings(record=True) as notices:
                warnings.simplefilter("always")
                config = manager.load()
            self.assertEqual(notices, [])
            self.assertTrue(any("no longer used" in notice for notice in manager.migration_notices))
            self.assertEqual(path.read_bytes(), original_bytes)
            self.assertEqual(config.dp5.score_threshold_percent, 17.5)
            self.assertTrue(config.dp5.keep_artifacts)
            manager.save(config)
            saved = path.read_text(encoding="utf-8")
            self.assertIn('# user configuration', saved)
            self.assertIn('# keep this path comment', saved)
            self.assertIn('score_threshold_percent = 17.5 # advisory threshold', saved)
            for obsolete in ("managed =", "install_root =", "expected_rev =", "probability_threshold_percent ="):
                self.assertNotIn(obsolete, saved)
            self.assertEqual(manager.load(), config)

    def test_unknown_legacy_looking_key_is_not_silently_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            path.write_text(CONFIG + 'install_directory = "unused"\n', encoding="utf-8")
            with self.assertRaises(ConfigValidationError):
                ConfigManager(path).load()


class DP5qCLIConfigTests(unittest.TestCase):
    """Resolve legacy dotted paths through the actual CLI parser and handlers."""

    def test_threshold_dotted_alias_has_a_migration_notice(self):
        import contextlib
        import io
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(self.cli._canonical_config_path("dp5.probability_threshold_percent"),
                             "dp5.score_threshold_percent")
        self.assertIn("Configuration notice:", output.getvalue())
        self.assertIn("score_threshold_percent", output.getvalue())

    def setUp(self) -> None:
        from checksi import main
        self.cli = main
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.directory) / "settings.toml"
        self.path.write_text(CONFIG + 'probability_threshold_percent = 12.0\n', encoding="utf-8")
        self.manager = ConfigManager(self.path)

    def run_config(self, *overrides):
        args = self.cli._build_parser().parse_args([
            "run", "input.pdf", *[arg for value in overrides for arg in ("--override", value)],
        ])
        return self.cli._build_run_config(self.manager.load(), args)

    def test_either_spelling_overrides_disk_without_mutating_file_or_base(self) -> None:
        before = self.path.read_bytes()
        for spelling in ("probability_threshold_percent", "score_threshold_percent"):
            with self.subTest(spelling=spelling):
                self.assertEqual(self.run_config(f"dp5.{spelling}=23").dp5.score_threshold_percent, 23)
        self.assertEqual(self.manager.load().dp5.score_threshold_percent, 12)
        self.assertEqual(self.path.read_bytes(), before)

    def test_only_conflicting_spellings_in_the_incoming_layer_fail(self) -> None:
        with self.assertRaisesRegex(self.cli.ConfigPathError, "Conflicting"):
            self.run_config("dp5.probability_threshold_percent=20", "dp5.score_threshold_percent=21")
        self.assertEqual(self.run_config(
            "dp5.probability_threshold_percent=20", "dp5.score_threshold_percent=20",
        ).dp5.score_threshold_percent, 20)
        for spelling in ("probability_threshold_percent", "score_threshold_percent"):
            with self.subTest(spelling=spelling):
                self.assertEqual(self.run_config(
                    f"dp5.{spelling}=20", f"dp5.{spelling}=21",
                ).dp5.score_threshold_percent, 21)

    def test_config_set_legacy_path_saves_canonical_keys(self) -> None:
        args = self.cli._build_parser().parse_args(["config", "set", "dp5.probability_threshold_percent", "27"])
        messages = []
        self.assertEqual(self.cli._handle_config_set(args, self.manager, log_callback=messages.append), 0)
        self.assertEqual(self.manager.load().dp5.score_threshold_percent, 27)
        self.assertNotIn("probability_threshold_percent", self.path.read_text(encoding="utf-8"))

    def test_doctor_uses_explicit_self_check_and_has_no_provisioning_guidance(self) -> None:
        messages = []
        with patch.object(self.cli, "doctor_runtime", return_value=(False, ["Asset hash mismatch"])) as doctor:
            self.assertEqual(self.cli._handle_dp5_doctor(self.manager, log_callback=messages.append), 1)
        doctor.assert_called_once()
        self.assertIn("Asset hash mismatch", messages)
        self.assertFalse(any("provision" in message or "setup.py" in message for message in messages))
