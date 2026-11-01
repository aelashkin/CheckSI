"""Public switch, legacy settings migration and local-only resolution policy."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from checksi.analysis import name_resolver as resolver
from checksi.config import ConfigManager, ConfigValidationError
from checksi.gui_main import parse_gui_arguments
from checksi.main import CLIUsageError, _build_parser, _build_run_config
from checksi.utils.error_handler import ErrorTracker
from tests.test_name_resolver import StubWorker


class DeepNameAnalysisTests(unittest.TestCase):
    def test_factory_and_public_cli_use_only_on_off_switch(self):
        cfg = ConfigManager.load_factory()
        self.assertEqual(cfg.model_dump(mode="json")["modes"].get("deep_name_analysis"), "off")
        self.assertNotIn("operating_mode", cfg.model_dump()["modes"])
        for mode in ("on", "off"):
            args = _build_parser().parse_args(["run", "input.pdf", "--deep-name-analysis", mode])
            self.assertEqual(_build_run_config(cfg, args).modes.deep_name_analysis, mode)
            gui_args = parse_gui_arguments(["--deep-name-analysis", mode])
            self.assertEqual(gui_args.deep_name_analysis, mode)
        with self.assertRaises(CLIUsageError):
            _build_parser().parse_args(["run", "input.pdf", "--operating-mode", "online"])

    def test_legacy_file_load_is_read_only_and_explicit_save_is_canonical(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            for legacy, canonical in (("offline", "off"), ("online", "on")):
                original = (f'[modes]\noperating_mode = "{legacy}"\noutput_mode = "essential"\n'
                            '[paths]\ninput_path = "input.pdf"\noutput_dir = "output"\n'
                            '[solvents]\nentries = []\n[errors]\ndefault_severity = "warning"\n')
                path.write_text(original)
                manager = ConfigManager(path)
                cfg = manager.load()
                self.assertEqual(getattr(cfg.modes, "deep_name_analysis", None), canonical)
                self.assertEqual(path.read_text(), original)
                manager.save(cfg)
                self.assertNotIn("operating_mode", path.read_text())
                self.assertIn(f'deep_name_analysis = "{canonical}"', path.read_text())
                self.assertEqual(ConfigManager(path).load(), cfg)

    def test_conflicting_legacy_and_new_switches_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.toml"
            path.write_text('[modes]\noperating_mode = "online"\ndeep_name_analysis = "off"\n'
                            'output_mode = "essential"\n[paths]\ninput_path = "i.pdf"\noutput_dir = "o"\n'
                            '[solvents]\nentries = []\n[errors]\ndefault_severity = "warning"\n')
            with self.assertRaisesRegex(ConfigValidationError, "Conflicting.*deep_name_analysis.*operating_mode"):
                ConfigManager(path).load()

    def test_off_does_not_start_worker_or_report_resolution_failure(self):
        cfg = ConfigManager.load_factory()
        details = {}
        with patch.object(resolver, "_ResolverWorker", side_effect=AssertionError("worker started")):
            result = resolver.smiles_and_name_from_paragraph(cfg, ErrorTracker(), "ethanol (1)", {}, details=details)
        self.assertEqual((result[0], result[1], result[4]), (None, "ethanol", []))
        self.assertEqual(details["status"], "disabled")

    def test_local_failure_never_dispatches_web_or_network_fallback(self):
        worker = StubWorker(lambda backend, name: {"status": "local_unavailable", "smiles": None})
        logs = []
        session = resolver._ResolverSession(logs.append)
        with patch.object(resolver, "_ResolverWorker", return_value=worker):
            self.assertIsNone(session.resolve("ethanol", resolver._Budget(1, None)))
            self.assertIsNone(session.resolve("methanol", resolver._Budget(1, None)))
        self.assertEqual(worker.calls, [("local", "ethanol")])
        self.assertEqual(len(logs), 1)
        self.assertIn("Local OPSIN unavailable", logs[0])
        self.assertNotIn("online", logs[0].lower())
        self.assertFalse(hasattr(resolver, "_web_smiles"))

    def test_local_initialization_failure_has_explicit_reason_and_no_speculative_retries(self):
        cfg = ConfigManager.load_factory()
        cfg.modes.deep_name_analysis = "on"
        worker = StubWorker(lambda backend, name: {"status": "local_unavailable", "smiles": None})
        details = {}
        with patch.object(resolver, "_ResolverWorker", return_value=worker):
            result = resolver.smiles_and_name_from_paragraph(
                cfg, ErrorTracker(), "a-b-c-d-e-f-g-h-i-j (1)", {}, details=details,
            )
        self.assertIsNone(result[0])
        self.assertEqual((details["status"], details["reason"]), ("failed", "local_unavailable"))
        self.assertEqual(worker.calls, [("local", "a-b-c-d-e-f-g-h-i-j")])
