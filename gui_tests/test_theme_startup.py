"""Isolate native signal binding so a Qt crash becomes an actionable test failure."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest


class ThemeStartupTests(unittest.TestCase):
    def test_real_theme_hook_constructs_delivers_and_releases_window(self) -> None:
        script = r'''
import gc
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication
from checksi.config import ConfigManager
from checksi.gui.main_window import CheckSIMainWindow
from checksi.gui.theme import ThemeController, ThemePreference

app = QApplication([])
app.setQuitOnLastWindowClosed(False)
theme = ThemeController(app)
with patch.object(theme, "load_preference", return_value=ThemePreference.SYSTEM), \
     patch.object(CheckSIMainWindow, "_refresh_dp5_runtime_status"):
    window = CheckSIMainWindow(base_config=ConfigManager.load_factory(), theme_controller=theme)
    with patch.object(theme, "apply") as apply:
        QGuiApplication.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Dark)
        app.processEvents()
        assert apply.call_count == 1, apply.call_count
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        del window
        gc.collect()
        apply.reset_mock()
        QGuiApplication.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Light)
        app.processEvents()
        assert apply.call_count == 0, "System-theme signal retained the deleted window"
'''
        environment = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
        result = subprocess.run(
            [sys.executable, "-X", "faulthandler", "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-8000:])


if __name__ == "__main__":
    unittest.main()
