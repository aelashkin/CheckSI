"""Dropdown geometry must settle before it is shown, not between frames."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QRect
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QAbstractItemDelegate, QComboBox, QScrollArea,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from checksi.config import ConfigManager
from checksi.config_schema import ErrorSeverity
from checksi.errors.defs import AnalysisErrorCode
from checksi.gui.config_editor import ConfigEditorPage, ErrorSeverityDelegate
from checksi.gui.theme import ThemeController
from checksi.gui.widgets import FIELD_HEIGHT, StandardComboBox


class PopupTrace(QObject):
    def __init__(self, popup):
        super().__init__(popup)
        self.shown_geometry = None
        self.first_paint_geometry = None
        self.visible_changes = []
        popup.installEventFilter(self)

    def eventFilter(self, popup, event):
        if event.type() == QEvent.Type.Show:
            self.shown_geometry = QRect(popup.geometry())
        elif event.type() == QEvent.Type.Paint and self.first_paint_geometry is None:
            self.first_paint_geometry = QRect(popup.geometry())
        elif (self.shown_geometry is not None and popup.isVisible()
              and event.type() in (QEvent.Type.Move, QEvent.Type.Resize)):
            self.visible_changes.append(QRect(popup.geometry()))
        return False


class DropdownStabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def _release(self, widget):
        popup = self.app.activePopupWidget()
        if popup is not None:
            popup.hide()
        widget.close()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def _combo(self, *, dark=False, near_edge=False):
        surface = QWidget()
        surface.setStyleSheet(ThemeController._custom_overrides(dark))
        layout = QVBoxLayout(surface)
        combo = StandardComboBox()
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContentsOnFirstShow)
        combo.addItems(["Use Default", "Disabled", "Info", "Warning", "Critical"])
        combo.setCurrentIndex(4)
        layout.addWidget(combo)
        surface.resize(260, 70)
        screen = self.app.primaryScreen().availableGeometry()
        point = (QPoint(screen.right() - 290, screen.bottom() - 100) if near_edge
                 else screen.topLeft() + QPoint(90, 90))
        surface.move(point)
        surface.show()
        self.addCleanup(self._release, surface)
        self.app.processEvents()
        return combo

    def test_popup_keeps_its_first_visible_geometry_in_both_themes(self):
        for dark in (False, True):
            with self.subTest(dark=dark):
                combo = self._combo(dark=dark)
                popup = combo.view().window()
                trace = PopupTrace(popup)
                combo.showPopup()
                QTest.qWait(70)
                self.assertIsNotNone(trace.first_paint_geometry)
                self.assertEqual(trace.visible_changes, [])
                self.assertEqual(trace.first_paint_geometry, trace.shown_geometry)
                self.assertEqual(popup.geometry(), trace.shown_geometry)
                self.assertTrue(combo.screen().availableGeometry().contains(popup.geometry()))
                combo.hidePopup()

    def test_popup_near_bottom_right_stays_inside_available_screen(self):
        combo = self._combo(near_edge=True)
        popup = combo.view().window()
        combo.showPopup()
        QTest.qWait(70)
        available = combo.screen().availableGeometry()
        self.assertTrue(available.contains(popup.geometry()), (available, popup.geometry()))
        self.assertLess(popup.y(), combo.mapToGlobal(QPoint()).y())
        combo.hidePopup()

    def test_severity_editor_retains_cell_geometry_after_queued_sizing(self):
        page = ConfigEditorPage()
        self.addCleanup(self._release, page)
        config = ConfigManager.load_factory()
        config.errors.overrides[AnalysisErrorCode.HRMS_ION_CHARGE_MISSING] = ErrorSeverity.CRITICAL
        page.load_from_config(config)
        page.resize(1000, 700)
        page.errors_section.set_expanded(True)
        page._set_error_overrides_visible(True)
        page.show()
        self.app.processEvents()
        scroll = page.findChild(QScrollArea)
        scroll.verticalScrollBar().setValue(page.error_search_edit.mapTo(scroll.widget(), QPoint()).y() - 16)
        table = page.errors_table
        row = list(AnalysisErrorCode).index(AnalysisErrorCode.HRMS_ION_CHARGE_MISSING)
        index = table.model().index(row, 1)
        table.scrollTo(index)
        table.edit(index)
        self.app.processEvents()
        editor = table.findChild(StandardComboBox, "errorSeverityEditor")
        self.assertIsNotNone(editor)
        cell = table.visualRect(index)
        margin = max((cell.height() - FIELD_HEIGHT) // 2, 0)
        expected = cell.adjusted(0, margin, 0, -margin)
        expected.setHeight(FIELD_HEIGHT)
        QTest.qWait(70)
        self.assertEqual(editor.geometry(), expected)
        self.assertEqual(page.build_config(), config)
        self.assertFalse(page.has_unconfirmed_changes())

    def test_cancelled_editor_does_not_open_a_queued_popup(self):
        table = QTableWidget(1, 2)
        self.addCleanup(self._release, table)
        table.setItem(0, 0, QTableWidgetItem("example_error"))
        table.setItem(0, 1, QTableWidgetItem("Use Default"))
        delegate = ErrorSeverityDelegate(table)
        table.setItemDelegateForColumn(1, delegate)
        table.show()
        self.app.processEvents()
        table.edit(table.model().index(0, 1))
        editor = table.findChild(StandardComboBox, "errorSeverityEditor")
        self.assertIsNotNone(editor)
        trace = PopupTrace(editor.view().window())
        delegate.closeEditor.emit(editor, QAbstractItemDelegate.EndEditHint.RevertModelCache)
        self.app.processEvents()
        QTest.qWait(70)
        self.assertIsNone(trace.shown_geometry)
        self.assertEqual(table.item(0, 1).text(), "Use Default")


if __name__ == "__main__":
    unittest.main()
