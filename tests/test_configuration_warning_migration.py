"""Old GUI/CLI user files gain only the two conditional-warning defaults."""

from importlib import resources
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from checksi.config import ConfigManager
from checksi.config_schema import ErrorSeverity
from checksi.errors import ErrorCode
from checksi.errors.manager import get_error_severity


CODES = (ErrorCode.NMR_CONFIGURATION_DEPENDENT, ErrorCode.DP5_CONFIGURATION_DEPENDENT)


class ConfigurationWarningMigrationTests(unittest.TestCase):
    def old_settings(self, extra=''):
        text = resources.files('checksi').joinpath('defaults.toml').read_text(encoding='utf-8')
        for code in CODES:
            text = text.replace(f'{code.value} = "warning"\n', '')
        return text + extra

    def test_old_file_seeds_only_new_warnings_in_memory_with_notice_and_explicit_save(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.toml'
            before = self.old_settings().encode()
            path.write_bytes(before)
            manager = ConfigManager(path)
            config = manager.load()
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(config.errors.default_severity, ErrorSeverity.CRITICAL)
            self.assertEqual(get_error_severity(config, ErrorCode.NMR_SIGNAL_COUNT_STRUCTURE_MISMATCH),
                             ErrorSeverity.CRITICAL)
            for code in CODES:
                self.assertEqual(get_error_severity(config, code), ErrorSeverity.WARNING)
            self.assertTrue(any('configuration' in notice and 'warning' in notice
                                for notice in manager.migration_notices))
            manager.save(config)
            self.assertNotEqual(path.read_bytes(), before)
            reloaded = ConfigManager(path)
            self.assertEqual(reloaded.load(), config)
            self.assertEqual(reloaded.migration_notices, ())

    def test_explicit_critical_disabled_and_info_overrides_are_kept(self):
        for severity in ('critical', 'disabled', 'info'):
            with self.subTest(severity=severity), TemporaryDirectory() as directory:
                path = Path(directory) / 'settings.toml'
                before = self.old_settings(''.join(f'{code.value} = "{severity}"\n' for code in CODES)).encode()
                path.write_bytes(before)
                manager = ConfigManager(path)
                config = manager.load()
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(manager.migration_notices, ())
                for code in CODES:
                    self.assertEqual(get_error_severity(config, code), ErrorSeverity(severity))


if __name__ == '__main__':
    unittest.main()
