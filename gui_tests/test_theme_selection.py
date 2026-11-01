"""Theme choices apply and persist independently from analytical settings."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtWidgets import QApplication

from checksi.config import ConfigManager
from checksi.gui.main_window import AppSettingsPage, CheckSIMainWindow
from checksi.gui.theme import ThemeController, ThemePreference


class ThemeSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_selector_emits_preferences_after_qt_converts_enum_data_to_strings(self):
        page = AppSettingsPage(ThemePreference.SYSTEM)
        self.addCleanup(page.deleteLater)
        changes = []
        page.theme_changed.connect(changes.append)
        for preference in (ThemePreference.DARK, ThemePreference.LIGHT, ThemePreference.SYSTEM):
            page.theme_combo.setCurrentIndex(page.theme_combo.findData(preference.value))
        self.assertEqual(changes, ["dark", "light", "system"])

    def test_dark_selection_applies_and_saves_without_confirming_analysis_settings(self):
        self.addCleanup(self.app.setStyleSheet, self.app.styleSheet())
        self.addCleanup(self.app.setPalette, self.app.palette())
        for name in ("themeDark", "themePreference"):
            self.addCleanup(self.app.setProperty, name, self.app.property(name))
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "appearance.ini")
            controller = ThemeController(self.app)
            self.addCleanup(controller.deleteLater)
            controller._settings = QSettings(path, QSettings.Format.IniFormat)
            config = ConfigManager.load_factory()
            with patch("checksi.gui.theme.build_stylesheet", None), \
                 patch.object(CheckSIMainWindow, "_refresh_dp5_runtime_status"):
                window = CheckSIMainWindow(base_config=config, theme_controller=controller)
                try:
                    editor = window.config_editor_page
                    combo = window.app_settings_page.theme_combo
                    self.assertFalse(editor.confirm_button.isEnabled())
                    with patch.object(ConfigManager, "save") as analysis_save:
                        combo.setCurrentIndex(combo.findData(ThemePreference.DARK.value))
                    self.assertTrue(self.app.property("themeDark"))
                    self.assertEqual(self.app.property("themePreference"), "dark")
                    self.assertEqual(controller.load_preference(), ThemePreference.DARK)
                    controller._settings.sync()
                    fresh_settings = QSettings(path, QSettings.Format.IniFormat)
                    self.assertEqual(fresh_settings.value(controller.SETTINGS_KEY), "dark")
                    self.assertFalse(editor.has_unconfirmed_changes())
                    self.assertFalse(editor.confirm_button.isEnabled())
                    self.assertEqual(editor.confirmed_config(), config)
                    analysis_save.assert_not_called()
                finally:
                    window._shutdown_requested = True
                    window.close()
                    window.deleteLater()
                    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    unittest.main()
