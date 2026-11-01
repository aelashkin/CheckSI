"""Desktop shell behavior without document processing or persisted preferences."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QLineEdit, QScrollArea

from checksi.config import ConfigManager
from checksi.config_schema import DeepNameAnalysis
from checksi.gui import widgets
from checksi.gui.config_editor import ConfigEditorPage
from checksi.gui.main_window import DashboardPage, CheckSIMainWindow
from checksi.gui.theme import ThemeController, ThemePreference


class DesktopShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self) -> None:
        self.config = ConfigManager.load_factory()
        theme = Mock()
        theme.load_preference.return_value = ThemePreference.SYSTEM
        self.enterContext(patch.object(CheckSIMainWindow, "_refresh_dp5_runtime_status"))
        self.window = CheckSIMainWindow(base_config=self.config, theme_controller=theme)
        self.addCleanup(self.window.deleteLater)
        self.addCleanup(self.window.close)
        self.addCleanup(setattr, self.window, "_shutdown_requested", True)

    def test_packaged_icon_path_remains_importable_outside_working_directory(self) -> None:
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                from checksi.gui.main_window import APP_ICON_PATH

                self.assertTrue(APP_ICON_PATH.is_absolute())
                with APP_ICON_PATH.open("rb") as icon:
                    self.assertEqual(icon.read(8), b"\x89PNG\r\n\x1a\n")
            finally:
                os.chdir(previous)

    def test_two_destinations_keep_appearance_with_analytical_settings(self) -> None:
        self.assertEqual(self.window.page_stack.count(), 2)
        self.window.settings_nav_button.click()
        self.assertIs(self.window.page_stack.currentWidget(), self.window.config_editor_page)
        self.assertTrue(self.window.config_editor_page.isAncestorOf(self.window.app_settings_page))
        self.window.analyze_nav_button.click()
        self.assertIs(self.window.page_stack.currentWidget(), self.window.dashboard_page)

    def test_laptop_layout_can_resize_to_1024_by_700_without_horizontal_scrolling(self) -> None:
        self.window.show()
        self.window.resize(1024, 700)
        self.app.processEvents()
        self.assertEqual(self.window.size(), QSize(1024, 700))
        for page in (self.window.dashboard_page, self.window.config_editor_page):
            self.window.page_stack.setCurrentWidget(page)
            self.app.processEvents()
            for scroll in page.findChildren(QScrollArea):
                self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
        self.assertTrue(self.window.settings_nav_button.isVisible())

    def test_startup_window_fits_available_screen_including_small_desktops(self) -> None:
        for width, height in ((1366, 768), (1024, 700), (800, 600)):
            with self.subTest(available=(width, height)):
                screen = Mock()
                screen.availableGeometry.return_value = QRect(0, 0, width, height)
                with patch.object(self.window, "screen", return_value=screen):
                    self.window._apply_window_geometry()
                self.assertLessEqual(self.window.width(), width - 24)
                self.assertLessEqual(self.window.height(), height - 48)
                self.assertLessEqual(self.window.minimumWidth(), min(1024, width - 24))
                self.assertLessEqual(self.window.minimumHeight(), min(700, height - 48))

    def test_all_checkboxes_are_keyboard_focusable_and_space_toggles_them(self) -> None:
        self.window.show()
        for page in (self.window.dashboard_page, self.window.config_editor_page):
            self.window.page_stack.setCurrentWidget(page)
            self.app.processEvents()
            for checkbox in page.findChildren(QCheckBox):
                with self.subTest(checkbox=checkbox.text()):
                    self.assertTrue(checkbox.focusPolicy() & Qt.FocusPolicy.TabFocus)
                    before = checkbox.isChecked()
                    QTest.keyClick(checkbox, Qt.Key.Key_Space)
                    self.assertEqual(checkbox.isChecked(), not before)

    def test_initial_selectors_and_factory_startup_have_deep_analysis_off(self) -> None:
        dashboard = DashboardPage()
        editor = ConfigEditorPage()
        self.addCleanup(dashboard.deleteLater)
        self.addCleanup(editor.deleteLater)
        self.assertIs(dashboard.deep_name_analysis_control.current_data(), DeepNameAnalysis.OFF)
        self.assertIs(editor.default_deep_name_analysis_control.current_data(), DeepNameAnalysis.OFF)
        self.assertIs(self.config.modes.deep_name_analysis, DeepNameAnalysis.OFF)
        self.assertIs(self.window.dashboard_page.deep_name_analysis_control.current_data(), DeepNameAnalysis.OFF)

    def test_explicit_deep_analysis_on_is_preserved_in_both_destinations(self) -> None:
        self.config.modes.deep_name_analysis = DeepNameAnalysis.ON
        self.window._load_config_into_ui(self.config, preserve_dashboard_input=False)
        self.assertIs(self.window.dashboard_page.deep_name_analysis_control.current_data(), DeepNameAnalysis.ON)
        self.assertIs(self.window.config_editor_page.build_config().modes.deep_name_analysis, DeepNameAnalysis.ON)

    def test_visible_form_labels_are_associated_with_their_controls(self) -> None:
        editor = self.window.config_editor_page
        for control in (editor.input_path_edit, editor.output_dir_edit,
                        editor.pdf_header_margin_spinbox, editor.dp5_threshold_spinbox):
            with self.subTest(control=control):
                self.assertTrue(control.accessibleName())
                self.assertTrue(any(label.buddy() is control for label in editor.findChildren(QLabel)))

    def test_disclosure_has_keyboard_control_and_hides_its_contents(self) -> None:
        section_type = getattr(widgets, "CollapsibleSection", None)
        self.assertIsNotNone(section_type, "Shared disclosure is required for activity and advanced settings")
        section = section_type("Advanced settings")
        self.addCleanup(section.close)
        field = QLineEdit()
        section.content_layout.addWidget(field)
        section.show()
        self.app.processEvents()
        self.assertFalse(section.is_expanded())
        self.assertTrue(section.content.isHidden())
        self.assertTrue(section.toggle_button.focusPolicy() & Qt.FocusPolicy.TabFocus)
        changes = []
        section.expanded_changed.connect(changes.append)
        QTest.keyClick(section.toggle_button, Qt.Key.Key_Space)
        self.assertTrue(section.is_expanded())
        self.assertFalse(section.content.isHidden())
        section.set_expanded(False)
        self.assertEqual(changes, [True, False])

    def test_navigation_icons_render_at_multiple_sizes_in_both_states(self) -> None:
        for button in (self.window.analyze_nav_button, self.window.settings_nav_button):
            self.assertFalse(button.icon().isNull())
            for size in (16, 32, 64):
                for state in (QIcon.State.Off, QIcon.State.On):
                    pixmap = button.icon().pixmap(QSize(size, size), QIcon.Mode.Normal, state)
                    self.assertEqual(pixmap.size(), QSize(size, size))
                    pixels = pixmap.toImage()
                    self.assertTrue(all(pixels.pixelColor(x, 0).alpha() == 0 for x in range(size)),
                                    "Vector icons must paint onto a transparent initialized surface")

    def test_theme_owned_normal_text_has_readable_contrast(self) -> None:
        color_factory = getattr(ThemeController, "colors", None)
        self.assertIsNotNone(color_factory, "Theme tokens must keep foreground/background pairs explicit")
        for dark in (False, True):
            colors = color_factory(dark)
            for foreground, background in (
                ("on_surface", "surface"),
                ("muted", "surface"),
                ("muted", "surface_variant"),
                ("secondary_text", "secondary"),
                ("button_text", "primary"),
                ("button_text", "danger"),
                ("warning_text", "warning"),
                ("critical_text", "surface"),
                ("warning_text_on_surface", "surface"),
                ("info_text", "surface"),
            ):
                with self.subTest(dark=dark, foreground=foreground, background=background):
                    self.assertGreaterEqual(self._contrast(colors[foreground], colors[background]), 4.5)

    @staticmethod
    def _contrast(first: str, second: str) -> float:
        def luminance(raw: str) -> float:
            values = [int(raw[index:index + 2], 16) / 255 for index in (1, 3, 5)]
            values = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in values]
            return sum(v * weight for v, weight in zip(values, (0.2126, 0.7152, 0.0722)))

        light, dark = sorted((luminance(first), luminance(second)), reverse=True)
        return (light + 0.05) / (dark + 0.05)


if __name__ == "__main__":
    unittest.main()
