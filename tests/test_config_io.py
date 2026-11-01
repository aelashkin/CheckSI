"""Settings file persistence and load diagnostics through the real ConfigManager."""

from contextlib import contextmanager
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from checksi.config import ConfigConflictError, ConfigIOError, ConfigManager, ConfigValidationError
from checksi.config_schema import ErrorSeverity
from checksi.errors.defs import AnalysisErrorCode

ROOT_KEYS = """\
paths.input_path = "input.pdf"
paths.output_dir = "report.docx"
dp5.enabled = false
dp5.timeout_seconds = 600
"""

TABLES = """
[modes]
deep_name_analysis = "off"
output_mode = "essential"

[[solvents.entries]]
name = "CDCl3"
aliases = []
proton_shifts = [7.26]
carbon_shifts = [77.0]

[errors]
default_severity = "warning"
"""

DOTTED_SETTINGS = ROOT_KEYS + TABLES


class ConfigPersistenceTests(unittest.TestCase):
    """Saving must keep every valid layout loadable and keep file attributes."""

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.tmp / "settings.toml"
        self.path.write_text(DOTTED_SETTINGS, encoding="utf-8")

    def test_stale_second_client_cannot_erase_unrelated_first_client_edit(self):
        first = ConfigManager(self.path)
        second = ConfigManager(self.path)
        first_config = first.load()
        second_config = second.load()
        first_config.dp5.timeout_seconds = 900
        first.save(first_config)
        second_config.modes.output_mode = "detailed"
        with self.assertRaises(ConfigConflictError):
            second.save(second_config)
        saved = ConfigManager(self.path).load()
        self.assertEqual(saved.dp5.timeout_seconds, 900)
        self.assertEqual(saved.modes.output_mode, "essential")
        self.assertEqual(second_config.modes.output_mode, "detailed")

    def test_comments_and_file_deletion_are_revision_conflicts(self):
        for change in ("comment", "delete"):
            with self.subTest(change=change):
                self.path.write_text(DOTTED_SETTINGS, encoding="utf-8")
                manager = ConfigManager(self.path)
                config = manager.load()
                if change == "comment":
                    self.path.write_text("# External note\n" + DOTTED_SETTINGS, encoding="utf-8")
                else:
                    self.path.unlink()
                with self.assertRaises(ConfigConflictError):
                    manager.save(config)
                self.assertEqual(self.path.exists(), change == "comment")

    def test_factory_snapshot_detects_external_creation(self):
        self.path.unlink()
        with patch("checksi.config.user_config_path", return_value=self.path):
            manager = ConfigManager()
            config = manager.load()
            ConfigManager(self.path).save(config)
            original = self.path.read_bytes()
            config.dp5.timeout_seconds = 900
            with self.assertRaises(ConfigConflictError):
                manager.save(config)
        self.assertEqual(self.path.read_bytes(), original)

    def test_successful_saves_refresh_revision_without_a_schema_field(self):
        self.path.write_text("# Keep this note\n" + DOTTED_SETTINGS, encoding="utf-8")
        manager = ConfigManager(self.path)
        config = manager.load()
        for timeout in (700, 900):
            config.dp5.timeout_seconds = timeout
            manager.save(config)
            self.assertEqual(ConfigManager(self.path).load(), config)
        self.assertTrue(self.path.read_text(encoding="utf-8").startswith("# Keep this note\n"))
        self.assertNotIn("revision", self.path.read_text(encoding="utf-8"))

    def test_failed_save_retains_prior_revision_and_unsaved_config(self):
        manager = ConfigManager(self.path)
        config = manager.load()
        original = self.path.read_bytes()
        config.dp5.timeout_seconds = 900
        with patch.object(manager, "_atomic_write", side_effect=ConfigIOError("Write refused")):
            with self.assertRaises(ConfigIOError):
                manager.save(config)
        self.assertEqual(self.path.read_bytes(), original)
        self.path.write_text("# External note\n" + DOTTED_SETTINGS, encoding="utf-8")
        with self.assertRaises(ConfigConflictError):
            manager.save(config)
        self.assertEqual(config.dp5.timeout_seconds, 900)

    def test_transaction_preserves_layout_and_permission_bits_through_symlink(self):
        target = self.path
        if os.name == "posix":
            os.chmod(target, 0o640)
            alias = self.tmp / "alias.toml"
            alias.symlink_to(target)
        else:
            alias = target
        target.write_text("# Transaction note\n" + DOTTED_SETTINGS, encoding="utf-8")
        manager = ConfigManager(alias)
        manager.update(lambda config: setattr(config.dp5, "timeout_seconds", 900))
        self.assertEqual(ConfigManager(target).load().dp5.timeout_seconds, 900)
        saved = target.read_text(encoding="utf-8")
        self.assertTrue(saved.startswith("# Transaction note\n"))
        self.assertIn("dp5.timeout_seconds = 900", saved)
        if os.name == "posix":
            self.assertTrue(alias.is_symlink())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    def test_invalid_transaction_leaves_file_unchanged(self):
        manager = ConfigManager(self.path)
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            manager.update(lambda config: setattr(config.dp5, "timeout_seconds", 0))
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_transaction_preserves_existing_shared_lock(self):
        manager = ConfigManager(self.path)
        manager.update(lambda config: None)
        lock_stat = manager.lock_path.stat()
        self.path.unlink()
        with self.assertRaisesRegex(ConfigIOError, "Configuration file not found"):
            manager.update(lambda config: None)
        self.assertEqual(manager.lock_path.stat().st_ino, lock_stat.st_ino)
        self.assertFalse(self.path.exists())

    def test_transaction_rechecks_missing_settings_inside_the_lock(self):
        manager = ConfigManager(self.path)
        file_lock = manager._file_lock
        changes = []

        @contextmanager
        def delete_after_lock(*, exclusive):
            with file_lock(exclusive=exclusive):
                self.path.unlink()
                yield

        with patch.object(manager, "_file_lock", delete_after_lock):
            with self.assertRaisesRegex(ConfigIOError, "Configuration file not found"):
                manager.update(changes.append)
        self.assertEqual(changes, [])
        self.assertFalse(self.path.exists())
        self.assertTrue(manager.lock_path.exists())

    def test_tables_written_as_dotted_keys_reload_after_save(self):
        manager = ConfigManager(self.path)
        loaded = manager.load()
        loaded.dp5.timeout_seconds = 900
        manager.save(loaded)
        reloaded = ConfigManager(self.path).load()
        self.assertEqual(reloaded.dp5.timeout_seconds, 900)
        self.assertEqual(reloaded.paths.output_dir, Path("report.docx"))
        self.assertNotIn("dp5.dp5", self.path.read_text(encoding="utf-8"))

    def test_saves_that_remove_keys_from_dotted_tables_reload(self):
        legacy_dp5 = ROOT_KEYS + 'dp5.managed = true\ndp5.install_root = ".runtime/dp5"\n'
        overrides_in_table = TABLES + 'overrides.nmr_solvent_and_frequency_missing = "info"\noverrides.hrms_formula_missing = "warning"\n'
        tables_without_errors = TABLES.replace('\n[errors]\ndefault_severity = "warning"\n', "")
        root_overrides = (ROOT_KEYS + 'errors.default_severity = "warning"\n'
                          'errors.overrides.formula_atom_symbol_invalid = "info"\n'
                          'errors.overrides.hrms_formula_missing = "warning"\n' + tables_without_errors)
        for name, text, drop in [
            ("legacy dp5 keys", legacy_dp5 + TABLES, None),
            ("partial override removal", ROOT_KEYS + overrides_in_table, "hrms_formula_missing"),
            ("root dotted overrides", root_overrides, "hrms_formula_missing"),
        ]:
            with self.subTest(layout=name):
                self.path.write_text(text, encoding="utf-8")
                manager = ConfigManager(self.path)
                loaded = manager.load()
                loaded.dp5.timeout_seconds = 900
                if drop:
                    loaded.errors.overrides = {code: severity for code, severity in loaded.errors.overrides.items()
                                               if code.value != drop}
                manager.save(loaded)
                reloaded = ConfigManager(self.path).load()
                self.assertEqual(reloaded.dp5.timeout_seconds, 900)
                self.assertEqual(reloaded.errors.overrides, loaded.errors.overrides)
                saved = self.path.read_text(encoding="utf-8")
                self.assertNotIn("install_root", saved)
                if drop:
                    self.assertNotIn(drop, saved)

    def test_inline_table_followed_by_dotted_keys_reloads_after_save(self):
        text = ('dp5 = {enabled = true}\nmodes.deep_name_analysis = "off"\nmodes.output_mode = "essential"\n'
                'paths.input_path = "i.pdf"\npaths.output_dir = "r.docx"\n'
                + TABLES.replace('\n[modes]\ndeep_name_analysis = "off"\noutput_mode = "essential"\n', ""))
        self.path.write_text(text, encoding="utf-8")
        manager = ConfigManager(self.path)
        loaded = manager.load()
        manager.save(loaded)
        self.assertEqual(ConfigManager(self.path).load(), loaded)

    def test_dotted_tables_that_need_a_section_header_reload_after_save(self):
        # [[solvents.entries]] or [errors.overrides] written where dotted keys
        # stood would claim every root key after them, here the dp5 keys.
        inline = ('paths = {input_path = "input.pdf", output_dir = "report.docx"}\n'
                  'modes = {deep_name_analysis = "off", output_mode = "essential"}\n')
        dp5 = "dp5.enabled = false\ndp5.timeout_seconds = 600\n"
        dotted_errors = 'errors.default_severity = "warning"\nsolvents.entries = []\n'
        for name, text, add_override in [
            ("dotted solvent entries", inline + 'errors = {default_severity = "warning"}\n'
             'solvents.entries = [{name = "CDCl3", carbon_shifts = [77.0]}]\n' + dp5, False),
            ("dotted errors without overrides", inline + dotted_errors + dp5, False),
            ("dotted errors gaining an override", inline + dotted_errors + dp5, True),
            ("dotted errors with inline overrides", inline + dotted_errors
             + 'errors.overrides = {hrms_formula_missing = "info"}\n' + dp5, False),
        ]:
            with self.subTest(layout=name):
                self.path.write_text(text, encoding="utf-8")
                manager = ConfigManager(self.path)
                loaded = manager.load()
                loaded.dp5.timeout_seconds = 900
                if add_override:
                    loaded.errors.overrides = {AnalysisErrorCode("hrms_formula_missing"): ErrorSeverity.CRITICAL}
                manager.save(loaded)
                expected = loaded.model_copy(deep=True)
                for code in (AnalysisErrorCode.NMR_CONFIGURATION_DEPENDENT, AnalysisErrorCode.DP5_CONFIGURATION_DEPENDENT):
                    expected.errors.overrides.setdefault(code, ErrorSeverity.WARNING)
                self.assertEqual(ConfigManager(self.path).load(), expected)

    def test_saving_without_solvents_keeps_the_file_loadable(self):
        manager = ConfigManager(self.path)
        loaded = manager.load()
        loaded.solvents.entries = []
        manager.save(loaded)
        self.assertEqual(ConfigManager(self.path).load().solvents.entries, [])

    def test_validation_error_names_each_failing_setting(self):
        text = DOTTED_SETTINGS.replace('output_mode = "essential"', 'output_mode = "essentail"\nheader_margin = 0.9')
        text += 'open_report_on_success = true\n'  # Misspelt key appended under [errors].
        self.path.write_text(text, encoding="utf-8")
        with self.assertRaises(ConfigValidationError) as error:
            ConfigManager(self.path).load()
        message = str(error.exception)
        self.assertIn(str(self.path.resolve()), message)
        for location in ("modes.output_mode", "modes.header_margin", "errors.open_report_on_success"):
            self.assertIn(location, message)

    def test_conflicting_threshold_spellings_are_explained(self):
        conflicting = "dp5.probability_threshold_percent = 5\ndp5.score_threshold_percent = 10\n"
        self.path.write_text(ROOT_KEYS + conflicting + TABLES, encoding="utf-8")
        with self.assertRaises(ConfigValidationError) as error:
            ConfigManager(self.path).load()
        self.assertIn("Conflicting DP5 threshold settings", str(error.exception))

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits")
    def test_save_keeps_existing_file_mode(self):
        for mode in (0o644, 0o664, 0o640):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.path, mode)
                manager = ConfigManager(self.path)
                manager.save(manager.load())
                self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), mode)

    @unittest.skipUnless(hasattr(os, "symlink") and os.name == "posix", "POSIX symlinks")
    def test_save_through_symlink_updates_target_and_keeps_link(self):
        target = self.tmp / "shared" / "settings.toml"
        target.parent.mkdir()
        self.path.rename(target)
        self.path.symlink_to(target)
        manager = ConfigManager(self.path)
        loaded = manager.load()
        loaded.dp5.timeout_seconds = 700
        manager.save(loaded)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(ConfigManager(target).load().dp5.timeout_seconds, 700)

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0, "POSIX directory permissions as a non-root user")
    def test_read_only_directory_allows_loading_but_not_saving(self):
        os.chmod(self.tmp, 0o555)
        self.addCleanup(os.chmod, self.tmp, 0o755)
        manager = ConfigManager(self.path)
        loaded = manager.load()
        self.assertFalse(manager.lock_path.exists())
        with self.assertRaises(ConfigIOError):
            manager.save(loaded)

    @unittest.skipUnless(hasattr(os, "symlink") and os.name == "posix", "POSIX symlinks")
    def test_symlink_and_target_share_one_lock(self):
        target = self.tmp / "shared" / "settings.toml"
        target.parent.mkdir()
        self.path.rename(target)
        self.path.symlink_to(target)
        self.assertEqual(ConfigManager(self.path).lock_path, ConfigManager(target).lock_path)
        self.assertEqual(ConfigManager(self.path).lock_path.parent, target.resolve().parent)

    @unittest.skipUnless(os.name == "posix", "POSIX advisory locks")
    def test_reader_waits_for_a_writer_holding_a_lock_file_it_cannot_write(self):
        import fcntl

        manager = ConfigManager(self.path)
        manager.lock_path.touch()
        os.chmod(manager.lock_path, 0o444)
        finished = threading.Event()
        with open(manager.lock_path, "rb") as writer:
            fcntl.flock(writer.fileno(), fcntl.LOCK_EX)
            reader = threading.Thread(target=lambda: (manager.load(), finished.set()))
            reader.start()
            self.assertFalse(finished.wait(0.3))
            fcntl.flock(writer.fileno(), fcntl.LOCK_UN)
        reader.join(5)
        self.assertTrue(finished.is_set())


if __name__ == "__main__":
    unittest.main()
