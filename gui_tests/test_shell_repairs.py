"""Regressions for the four reviewed desktop-shell findings."""

from __future__ import annotations

import gc
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QSize, QThread, Qt
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel

from checksi.config import ConfigManager
from checksi.dp5q.client import DP5qAvailabilityError
from checksi.gui import widgets
from checksi.gui.config_editor import ConfigEditorPage
from checksi.gui.main_window import APP_ICON_PATH, DashboardPage, CheckSIMainWindow
from checksi.gui.theme import ThemeController, ThemePreference
from checksi.gui.widgets import CollapsibleSection, SingleSelectSegmentedButton, interface_icon
from checksi.gui.worker import AnalysisWorker


class ShellReviewRepairsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self) -> None:
        # Earlier fixtures close bare pages without deleting their C++ owners.
        # Reclaim inactive project widgets before application-wide style changes.
        gc.collect()
        for widget in self.app.topLevelWidgets():
            if widget.isVisible() or widget.parent() is not None:
                continue
            if isinstance(widget, CheckSIMainWindow):
                if not widget._shutdown_requested or any(thread.isRunning() for thread in widget.findChildren(QThread)):
                    continue
            elif not isinstance(widget, (ConfigEditorPage, DashboardPage)):
                continue
            widget.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.addCleanup(self.app.setPalette, self.app.palette())
        self.addCleanup(self.app.setStyleSheet, self.app.styleSheet())
        self.app.setStyleSheet("")

    def _window(self) -> CheckSIMainWindow:
        theme = Mock()
        theme.load_preference.return_value = ThemePreference.SYSTEM
        with patch.object(CheckSIMainWindow, "_refresh_dp5_runtime_status"):
            window = CheckSIMainWindow(base_config=ConfigManager.load_factory(), theme_controller=theme)

        def release_window() -> None:
            window._shutdown_requested = True
            window.close()
            window.deleteLater()
            self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

        self.addCleanup(release_window)
        return window

    def test_copied_icons_can_detach_after_the_original_is_collected(self) -> None:
        script = r'''
import gc
from pathlib import Path
import tempfile
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QApplication
from checksi.gui.widgets import interface_icon
app = QApplication([])
with tempfile.TemporaryDirectory() as directory:
    pixel = QPixmap(8, 8)
    pixel.fill()
    path = str(Path(directory) / "extra.png")
    assert pixel.save(path)
    for name in ("analyze", "settings", "disclosure"):
        original = interface_icon(name)
        first, second = QIcon(original), QIcon(original)
        first.addPixmap(pixel)
        second.addFile(path)
        del original
        gc.collect()
        assert not first.pixmap(24).isNull()
        assert not second.pixmap(24).isNull()
'''
        result = subprocess.run(
            [sys.executable, "-X", "faulthandler", "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-6000:])

    def test_interface_icons_supply_hidpi_mode_and_state_variants(self) -> None:
        for name in ("analyze", "settings", "disclosure"):
            icon = interface_icon(name)
            for mode in (QIcon.Mode.Normal, QIcon.Mode.Active, QIcon.Mode.Selected, QIcon.Mode.Disabled):
                for state in (QIcon.State.Off, QIcon.State.On):
                    for ratio in (1, 2, 3):
                        with self.subTest(name=name, mode=mode, state=state, ratio=ratio):
                            pixmap = icon.pixmap(QSize(24, 24), float(ratio), mode, state)
                            self.assertEqual(pixmap.size(), QSize(24 * ratio, 24 * ratio))
                            self.assertEqual(pixmap.devicePixelRatio(), ratio)
                            self.assertEqual(pixmap.toImage().pixelColor(0, 0).alpha(), 0)

    def test_navigation_and_disclosure_refresh_with_their_palette(self) -> None:
        window = self._window()
        section = CollapsibleSection("Details", window)
        for color in ("#172B42", "#D8E9FA"):
            palette = self.app.palette()
            palette.setColor(QPalette.ColorRole.WindowText, QColor(color))
            self.app.setPalette(palette)
            self.app.processEvents()
            for button in (window.analyze_nav_button, window.settings_nav_button, section.toggle_button):
                self.assertIn(color.lower(), self._opaque_colors(button.icon()))

    def test_window_icon_remains_the_packaged_logo_across_palette_changes(self) -> None:
        window = self._window()
        expected = QIcon(str(APP_ICON_PATH)).pixmap(64).toImage()
        for color in ("#172B42", "#D8E9FA"):
            palette = self.app.palette()
            palette.setColor(QPalette.ColorRole.WindowText, QColor(color))
            self.app.setPalette(palette)
            self.app.processEvents()
            self.assertEqual(window.windowIcon().pixmap(64).toImage(), expected)

    def test_styled_selected_navigation_keeps_the_highlighted_foreground(self) -> None:
        controller = ThemeController(self.app)
        controller._settings = Mock()
        self.addCleanup(controller.deleteLater)
        for name in ("themeDark", "themePreference"):
            self.addCleanup(self.app.setProperty, name, self.app.property(name))
        for preference in (ThemePreference.LIGHT, ThemePreference.DARK):
            controller.apply(preference)
            window = self._window()
            window.settings_nav_button.click()
            self.app.processEvents()
            foreground = self.app.palette().color(QPalette.ColorRole.HighlightedText).name()
            self.assertIn(foreground, self._opaque_colors(window.settings_nav_button.icon(), QIcon.State.On))

    def test_equivalent_palette_notifications_do_not_redraw_icons(self) -> None:
        window = self._window()
        button = window.settings_nav_button
        with patch("checksi.gui.widgets._interface_pixmap", wraps=widgets._interface_pixmap) as draw:
            for _ in range(3):
                self.app.sendEvent(button, QEvent(QEvent.Type.PaletteChange))
            self.assertEqual(draw.call_count, 0)
            palette = self.app.palette()
            palette.setColor(QPalette.ColorRole.WindowText, QColor("#32A175"))
            self.app.setPalette(palette)
            self.app.processEvents()
            self.assertGreater(draw.call_count, 0)
            self.assertIn("#32a175", self._opaque_colors(button.icon()))
            draw.reset_mock()
            for _ in range(3):
                self.app.sendEvent(button, QEvent(QEvent.Type.PaletteChange))
            self.assertEqual(draw.call_count, 0)

    def test_unavailable_dp5_guidance_names_the_current_settings_destination(self) -> None:
        window = self._window()
        self.assertIn("first eligible compound", window.dashboard_page.dp5_checkbox.toolTip())
        window.dashboard_page.set_dp5_runtime_status(False, [])
        tooltip = window.dashboard_page.dp5_checkbox.toolTip()
        self.assertIn("first eligible compound", tooltip)
        self.assertNotIn("before analysis", tooltip)
        failures = []
        worker = AnalysisWorker(config=ConfigManager.load_factory(), input_path="sample.pdf",
                                event_dispatcher=lambda _event: None, cancel_token=threading.Event())
        worker.failed.connect(lambda _operation, failure: failures.append(failure))
        with patch("checksi.gui.worker.ApplicationRunner.execute",
                   side_effect=DP5qAvailabilityError(["Fixture assets unavailable"])):
            worker.run()
        self.assertEqual(len(failures), 1)
        for origin, message in (("tooltip", tooltip), ("worker", failures[0].message)):
            with self.subTest(origin=origin):
                self.assertIn("Settings", message)
                self.assertIn("Refresh Status", message)
                self.assertNotIn("Configuration Editor", message)

    def test_segmented_labels_target_the_whole_field_and_name_every_option(self) -> None:
        window = self._window()
        editor = window.config_editor_page
        for field, control in (("Deep name analysis", editor.default_deep_name_analysis_control),
                               ("Output Mode", editor.default_output_mode_control),
                               ("Default Severity", editor.default_severity_control)):
            label = next(label for label in editor.findChildren(QLabel) if label.text() == field)
            self.assertIs(label.buddy(), control)
            for button in control.findChildren(SingleSelectSegmentedButton):
                self.assertIn(field, button.accessibleName())
                self.assertIn(button.text(), button.accessibleName())
        for field, control in (("Deep name analysis", window.dashboard_page.deep_name_analysis_control),
                               ("Output Mode", window.dashboard_page.output_mode_control)):
            label = next(label for label in window.dashboard_page.findChildren(QLabel) if label.text() == field)
            self.assertIs(label.buddy(), control)
            for button in control.findChildren(SingleSelectSegmentedButton):
                self.assertIn(field, button.accessibleName())

    def test_segmented_focus_tracks_pointer_and_arrow_key_selection(self) -> None:
        window = self._window()
        window.settings_nav_button.click()
        window.show()
        self.app.processEvents()
        for control in (window.config_editor_page.default_deep_name_analysis_control,
                        window.config_editor_page.default_output_mode_control,
                        window.config_editor_page.default_severity_control):
            buttons = control.findChildren(SingleSelectSegmentedButton)
            target = next(button for button in buttons if not button.isChecked())
            target.click()
            self.assertIs(control.focusProxy(), target)
            control.setFocus()
            self.app.processEvents()
            self.assertTrue(target.hasFocus())
            QTest.keyClick(target, Qt.Key.Key_Left if target is buttons[-1] else Qt.Key.Key_Right)
            selected = next(button for button in buttons if button.isChecked())
            self.assertIs(control.focusProxy(), selected)

    @staticmethod
    def _opaque_colors(icon: QIcon, state: QIcon.State = QIcon.State.Off) -> set[str]:
        image = icon.pixmap(QSize(24, 24), QIcon.Mode.Normal, state).toImage()
        return {image.pixelColor(x, y).name() for x in range(image.width())
                for y in range(image.height()) if image.pixelColor(x, y).alpha() == 255}


if __name__ == "__main__":
    unittest.main()
