"""The missing-material fallback uses the same stable popup base style."""

import os
from pathlib import Path
import subprocess
import sys
import unittest


SETUP = r'''
from pathlib import Path
import tempfile
from unittest.mock import patch
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QSettings
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QStyleFactory, QVBoxLayout, QWidget
from checksi.gui.theme import ThemeController, ThemePreference
from checksi.gui.widgets import StandardComboBox
'''


class ThemeFallbackTests(unittest.TestCase):
    def _probe(self, script):
        result = subprocess.run(
            [sys.executable, "-X", "faulthandler", "-c", SETUP + script],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-8000:] + result.stdout[-2000:])

    def test_fallback_initializes_fusion_once_across_theme_changes(self):
        self._probe(r'''
class App(QApplication):
    def __init__(self):
        self.styles = []
        super().__init__([])
    def setStyle(self, style):
        self.styles.append(style)
        return super().setStyle(style)
app = App()
with tempfile.TemporaryDirectory() as directory:
    for native in QStyleFactory.keys():
        app.setStyleSheet("")
        app.setStyle(native)
        app.styles.clear()
        controller = ThemeController(app)
        controller._settings = QSettings(str(Path(directory) / (native + '.ini')), QSettings.Format.IniFormat)
        with patch('checksi.gui.theme.build_stylesheet', None):
            for preference in (ThemePreference.LIGHT, ThemePreference.DARK, ThemePreference.SYSTEM):
                controller.apply(preference)
        assert app.styles == ['Fusion'], (native, app.styles)
        # A later material build must still register its fonts on its first use.
        with patch('checksi.gui.theme.build_stylesheet', return_value='QWidget {}') as build:
            controller.apply(ThemePreference.LIGHT)
        assert build.call_args.kwargs['export'] is False
        assert app.styles == ['Fusion']
        controller.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
''')

    def test_fallback_popup_matches_field_after_each_platform_style(self):
        self._probe(r'''
app = QApplication([])
app.setQuitOnLastWindowClosed(False)
with tempfile.TemporaryDirectory() as directory:
    for native in QStyleFactory.keys():
        app.setStyleSheet("")
        app.setStyle(native)
        controller = ThemeController(app)
        controller._settings = QSettings(str(Path(directory) / (native + '.ini')), QSettings.Format.IniFormat)
        with patch('checksi.gui.theme.build_stylesheet', None):
            controller.apply(ThemePreference.LIGHT)
        surface = QWidget()
        layout = QVBoxLayout(surface)
        combo = StandardComboBox(surface)
        combo.setSizeAdjustPolicy(StandardComboBox.SizeAdjustPolicy.AdjustToContentsOnFirstShow)
        combo.addItems(['Use Default', 'Disabled', 'Info', 'Warning', 'Critical'])
        combo.setCurrentIndex(4)
        layout.addWidget(combo)
        surface.resize(280, 80)
        surface.move(app.primaryScreen().availableGeometry().topLeft() + QPoint(90, 90))
        surface.show()
        app.processEvents()
        combo.showPopup()
        app.processEvents()
        popup = combo.view().window()
        initial = popup.geometry()
        QTest.qWait(70)
        field = combo.mapToGlobal(QPoint())
        bottom = combo.mapToGlobal(combo.rect().bottomLeft()).y()
        assert abs(popup.x() - field.x()) <= 1, (native, field, popup.geometry())
        assert popup.width() >= combo.width() - 1, (native, combo.width(), popup.width())
        assert bottom <= popup.y() <= bottom + 2, (native, bottom, popup.geometry())
        assert popup.geometry() == initial, native
        assert combo.screen().availableGeometry().contains(popup.geometry()), native
        combo.hidePopup()
        surface.close()
        surface.deleteLater()
        controller.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
''')


if __name__ == "__main__":
    unittest.main()
