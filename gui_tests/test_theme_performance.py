"""Bound expensive theme work and retain real diagnostic dropdown editing."""

import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScrollArea

from checksi.config import ConfigManager
from checksi.config_schema import ErrorSeverity
from checksi.errors.defs import AnalysisErrorCode
from checksi.gui.config_editor import ConfigEditorPage
from checksi.gui.widgets import StandardComboBox
from checksi.gui.theme import ThemeController, ThemePreference


class ThemeApplicationWorkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_each_switch_installs_one_stylesheet_without_resetting_the_native_style(self):
        self.addCleanup(self.app.setStyleSheet, self.app.styleSheet())
        self.addCleanup(self.app.setPalette, self.app.palette())
        for name in ("themeDark", "themePreference"):
            self.addCleanup(self.app.setProperty, name, self.app.property(name))
        controller = ThemeController(self.app)
        controller._settings = Mock()
        self.addCleanup(controller.deleteLater)
        with patch("checksi.gui.theme.build_stylesheet", return_value="QWidget {}") as build, \
             patch.object(self.app, "setStyleSheet", wraps=self.app.setStyleSheet) as sheet, \
             patch.object(self.app, "setStyle", wraps=self.app.setStyle) as native:
            for preference in (ThemePreference.LIGHT, ThemePreference.DARK, ThemePreference.LIGHT):
                controller.apply(preference)
        self.assertEqual(sheet.call_count, 3)
        self.assertEqual(native.call_count, 1)
        self.assertEqual([call.kwargs["export"] for call in build.call_args_list], [False, True, True])


class DiagnosticEditorPerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.page = ConfigEditorPage()
        self.config = ConfigManager.load_factory()
        self.page.load_from_config(self.config)
        self.addCleanup(self._dispose)

    def _dispose(self):
        popup = self.app.activePopupWidget()
        if popup is not None:
            popup.close()
        self.page.close()
        self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_diagnostics_do_not_allocate_a_popup_widget_for_every_error(self):
        self.assertLess(len(self.page.errors_table.findChildren(StandardComboBox)), 10)
        self.assertEqual(self.page.build_config(), self.config)

    def _open_editor(self, *, keyboard=False):
        self.page.resize(1000, 700)
        self.page.errors_section.set_expanded(True)
        self.page._set_error_overrides_visible(True)
        self.page.show()
        self.app.processEvents()
        self.page.findChild(QScrollArea).ensureWidgetVisible(self.page.error_search_edit)
        table = self.page.errors_table
        row = list(AnalysisErrorCode).index(AnalysisErrorCode.HRMS_ION_CHARGE_MISSING)
        item = table.item(row, 1)
        table.scrollToItem(item)
        table.setCurrentCell(row, 1)
        self.app.processEvents()
        if keyboard:
            table.setFocus()
            QTest.keyClick(table, Qt.Key.Key_Return)
        else:
            QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton,
                             pos=table.visualItemRect(item).center())
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        editor = table.findChild(StandardComboBox, "errorSeverityEditor")
        self.assertIsNotNone(editor)
        self.assertTrue(editor.view().isVisible())
        self.assertIn("hrms_ion_charge_missing", editor.accessibleName())
        return row, editor

    def _choose(self, editor, value):
        index = editor.findData(value)
        view = editor.view()
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton,
                         pos=view.visualRect(editor.model().index(index, 0)).center())
        self.app.processEvents()

    def test_one_click_edit_commits_and_reset_updates_the_changed_filter(self):
        row, editor = self._open_editor()
        self._choose(editor, ErrorSeverity.WARNING)
        self.assertEqual(self.page.build_config().errors.overrides[AnalysisErrorCode.HRMS_ION_CHARGE_MISSING],
                         ErrorSeverity.WARNING)
        self.page.changed_errors_checkbox.setChecked(True)
        self.assertFalse(self.page.errors_table.isRowHidden(row))
        _, editor = self._open_editor()
        self.assertEqual(editor.property("usingDefault"), "false")
        self._choose(editor, None)
        self.assertTrue(self.page.errors_table.isRowHidden(row))
        self.assertNotIn(AnalysisErrorCode.HRMS_ION_CHARGE_MISSING, self.page.build_config().errors.overrides)

    def test_keyboard_edit_keeps_the_severity_choice(self):
        _, editor = self._open_editor(keyboard=True)
        self._choose(editor, ErrorSeverity.DISABLED)
        self.assertEqual(self.page.build_config().errors.overrides[AnalysisErrorCode.HRMS_ION_CHARGE_MISSING],
                         ErrorSeverity.DISABLED)

    def test_escape_cancels_editing_without_changing_settings(self):
        _, editor = self._open_editor(keyboard=True)
        QTest.keyClick(editor.view(), Qt.Key.Key_Down)
        QTest.keyClick(editor.view(), Qt.Key.Key_Escape)
        QTest.keyClick(editor, Qt.Key.Key_Escape)
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(self.page.build_config(), self.config)
        self.assertFalse(self.page.has_unconfirmed_changes())
