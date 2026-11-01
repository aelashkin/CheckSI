"""File migration notices belong to configuration adapters, not warning filters."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import warnings

from checksi import main
from checksi.config import ConfigManager, ConfigValidationError
from tests.test_dp5q_config import CONFIG


class ConfigurationNoticeTests(unittest.TestCase):
    """Loading remains read-only and notices are deterministic on every load."""

    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(temporary) / "legacy.toml"
        self.path.write_text(CONFIG + 'probability_threshold_percent = 17.5\nmanaged = true\n')

    def test_manager_collects_each_load_without_python_warnings_or_writes(self):
        original = self.path.read_bytes()
        manager = ConfigManager(self.path)
        for _ in range(2):
            with warnings.catch_warnings(record=True) as observed:
                warnings.simplefilter("always")
                cfg = manager.load()
            self.assertEqual(observed, [])
            self.assertEqual(cfg.dp5.score_threshold_percent, 17.5)
            self.assertEqual(len(manager.migration_notices), 3)
            self.assertIn("score_threshold_percent", manager.migration_notices[0])
            self.assertIn("managed", manager.migration_notices[1])
            self.assertIn('nmr_configuration_dependent', manager.migration_notices[2])
            self.assertIn('dp5_configuration_dependent', manager.migration_notices[2])
            self.assertEqual(self.path.read_bytes(), original)
        self.path.write_text(CONFIG + 'unknown_key = true\n')
        with self.assertRaises(ConfigValidationError):
            manager.load()
        self.assertEqual(manager.migration_notices, ())

    def test_config_view_keeps_json_clean_and_prints_plain_stderr_notices(self):
        original = self.path.read_bytes()
        for _ in range(2):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main.main_cli(["--config-file", str(self.path), "config", "view"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["dp5"]["score_threshold_percent"], 17.5)
            self.assertEqual(stderr.getvalue().count("Configuration notice:"), 3)
            self.assertNotIn("pydantic", stderr.getvalue())
            self.assertNotIn("UserWarning", stderr.getvalue())
            self.assertEqual(self.path.read_bytes(), original)

    def test_rejected_config_set_still_emits_notices_from_its_successful_load(self):
        original = self.path.read_bytes()
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main.main_cli(["--config-file", str(self.path), "config", "set", "dp5.nonexistent", "true"])
        self.assertEqual(code, 1)
        self.assertIn("Error:", stdout.getvalue())
        self.assertEqual(stderr.getvalue().count("Configuration notice:"), 3)
        self.assertIn("score_threshold_percent", stderr.getvalue())
        self.assertIn("managed", stderr.getvalue())
        self.assertEqual(self.path.read_bytes(), original)

    def test_threshold_conflict_reaches_cli_with_field_names_without_raw_inputs(self):
        self.path.write_text(CONFIG + 'probability_threshold_percent = 17.5\nscore_threshold_percent = 12\n')
        stderr, stdout = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            code = main.main_cli(["--config-file", str(self.path), "config", "view"])
        self.assertEqual(code, 1)
        error = stderr.getvalue() + stdout.getvalue()
        self.assertIn("Conflicting DP5 threshold settings", error)
        self.assertIn("probability_threshold_percent", error)
        self.assertIn("score_threshold_percent", error)
        self.assertNotIn("input_value", error)

    def test_cli_dotted_alias_is_a_plain_notice_without_python_source_excerpt(self):
        self.path.write_text(CONFIG)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), warnings.catch_warnings(record=True) as observed:
            warnings.simplefilter("always")
            code = main.main_cli(["--config-file", str(self.path), "config", "set",
                                  "dp5.probability_threshold_percent", "12"])
        self.assertEqual(code, 0)
        self.assertEqual(observed, [])
        self.assertEqual(stderr.getvalue().count("Configuration notice:"), 2)
        self.assertNotIn("main.py:", stderr.getvalue())

    def test_direct_schema_migration_warning_points_to_the_external_caller(self):
        from checksi.config_schema import DP5Settings
        with warnings.catch_warnings(record=True) as observed:
            warnings.simplefilter("always")
            DP5Settings.model_validate({"probability_threshold_percent": 12})
        self.assertEqual(len(observed), 1)
        self.assertEqual(Path(observed[0].filename).resolve(), Path(__file__).resolve())


if __name__ == "__main__":
    unittest.main()
