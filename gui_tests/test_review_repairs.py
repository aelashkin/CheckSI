"""Dashboard regressions from the scoped GUI PR review."""

import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QTextCursor
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

from checksi.gui.dashboard import DashboardPage


class DashboardReviewRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.page = DashboardPage()
        self.page.resize(1024, 700)
        self.page.show()
        self.app.processEvents()
        self.addCleanup(self._close_page)

    def _close_page(self):
        from shiboken6 import Shiboken
        if Shiboken.isValid(self.page):
            self.page.close()
            self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def test_log_recolor_is_one_document_update_and_keeps_selection(self):
        document = self.page.log_output.document()
        text = "\n".join(f"Message {index}" for index in range(400))
        self.page.log_output.setPlainText(text)
        block = document.firstBlock()
        while block.isValid():
            block.setUserState(1)
            block = block.next()
        selection = QTextCursor(document)
        selection.setPosition(2)
        selection.setPosition(18, QTextCursor.MoveMode.KeepAnchor)
        self.page.log_output.setTextCursor(selection)
        selected = selection.selectedText()
        changes = QSignalSpy(document.contentsChanged)
        self.page.refresh_log_colors()
        self.assertEqual(changes.count(), 1)
        self.assertEqual(document.toPlainText(), text)
        self.assertEqual(self.page.log_output.textCursor().selectedText(), selected)

    def test_read_only_log_does_not_accumulate_undo_history(self):
        self.page.append_log("warning", "A warning")
        self.page.refresh_log_colors()
        self.assertFalse(self.page.log_output.document().isUndoRedoEnabled())
        self.assertFalse(self.page.log_output.document().isUndoAvailable())

    def _assert_deleted_page_drops_callback(self, schedule):
        errors = []
        with patch.object(sys, "excepthook", side_effect=lambda *args: errors.append(args)):
            schedule()
            self.page.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            self.app.processEvents()
        self.assertEqual(errors, [])

    def test_summary_reveal_is_cancelled_when_native_page_is_deleted(self):
        html = self.root / "findings.html"
        html.write_text("<p>Fixture</p>", encoding="utf-8")
        result = SimpleNamespace(input_path="sample.pdf", analysis_data={"total_found": 0},
            issue_counts={}, report_data={"html_path": str(html)})
        self._assert_deleted_page_drops_callback(lambda: self.page.show_summary(result, str(self.root)))

    def test_activity_reveal_is_cancelled_when_native_page_is_deleted(self):
        self._assert_deleted_page_drops_callback(lambda: self.page.activity_details.set_expanded(True))
