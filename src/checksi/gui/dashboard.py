"""Single-document analysis workspace and result presentation."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer, Qt, Signal, Slot
from PySide6.QtGui import QAction, QColor, QFontDatabase, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from checksi.assessment import ASSESSMENT_LABELS
from checksi.config_schema import AppConfig, DeepNameAnalysis, OutputMode
from checksi.gui.theme import ThemeController
from checksi.gui.widgets import (
    CollapsibleSection,
    FIELD_HEIGHT,
    SEGMENTED_CONTROL_HEIGHT,
    SingleSelectSegmentedControl,
    StandardButton,
    StandardLineEdit,
    SummaryBadge,
)
from checksi.utils.dp5_format import format_skip_reasons

if TYPE_CHECKING:
    from checksi.application_runner import ApplicationRunResult

class RunState(StrEnum):
    """UI run states that gate dashboard interactivity."""

    IDLE = "idle"
    RUNNING = "running"
    FINISHED = "finished"


class DashboardPage(QWidget):
    """Primary analysis workspace with live logs and summary actions."""

    browse_requested = Signal()
    output_browse_requested = Signal()
    run_requested = Signal()
    deep_name_analysis_changed = Signal(object)
    output_mode_changed = Signal(object)
    dp5_enabled_changed = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("pageSurface")
        self._has_started_run = False
        self._run_state = RunState.IDLE
        self._result_input_path = ""
        self._result_is_previous = False
        self._artifact_actions: dict[StandardButton, QAction] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        scroll_area = QScrollArea()
        self._scroll_area = scroll_area
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content = QWidget()
        content.setObjectName("pageSurface")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self._content_layout = layout

        input_card = QFrame()
        input_card.setProperty("card", "true")
        input_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        input_layout = QVBoxLayout(input_card)
        input_layout.setContentsMargins(16, 16, 16, 16)
        input_layout.setSpacing(12)

        self.input_title = QLabel("Choose a document")
        self.input_title.setTextFormat(Qt.TextFormat.PlainText)
        self.input_title.setWordWrap(True)
        self.input_title.setStyleSheet("font-size: 16px; font-weight: 600;")
        input_description = QLabel(
            "Choose the PDF or DOCX document you want to analyze for this run."
        )
        input_description.setWordWrap(True)
        input_description.setStyleSheet("opacity: 0.8;")
        input_layout.addWidget(self.input_title)
        input_layout.addWidget(input_description)

        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.setSpacing(14)
        self.input_path_edit = StandardLineEdit()
        self.input_path_edit.setReadOnly(True)
        self.input_path_edit.setAccessibleName("Selected document path")
        self.browse_button = StandardButton("Browse File", role="secondary")
        self.browse_button.setProperty("compactField", "true")
        self.browse_button.setFixedHeight(FIELD_HEIGHT)
        self.browse_button.clicked.connect(self.browse_requested)
        input_row.addWidget(self.input_path_edit, 1)
        input_row.addWidget(self.browse_button)
        input_layout.addLayout(input_row)
        output_row = QHBoxLayout()
        output_label = QLabel("Output folder")
        self.output_dir_edit = StandardLineEdit()
        self.output_dir_edit.setReadOnly(True)
        self.output_dir_edit.setAccessibleName("Output parent directory")
        output_label.setBuddy(self.output_dir_edit)
        self.output_browse_button = StandardButton("Change", role="secondary")
        self.output_browse_button.clicked.connect(self.output_browse_requested)
        output_row.addWidget(output_label)
        output_row.addWidget(self.output_dir_edit, 1)
        output_row.addWidget(self.output_browse_button)
        input_layout.addLayout(output_row)
        output_hint = QLabel("Each run creates a new folder here; existing reports stay available.")
        output_hint.setWordWrap(True)
        input_layout.addWidget(output_hint)
        self.input_error_label = QLabel()
        self.input_error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.input_error_label.setWordWrap(True)
        self.input_error_label.hide()
        input_layout.addWidget(self.input_error_label)
        layout.addWidget(input_card)

        session_card = CollapsibleSection("Run options · Deep name analysis: Off · Essential")
        self.options_section = session_card
        session_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        session_layout = session_card.content_layout
        session_layout.setSpacing(12)
        session_form = QFormLayout()
        session_form.setContentsMargins(0, 0, 0, 0)
        session_form.setSpacing(14)
        session_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        session_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.deep_name_analysis_control = self._build_enum_segments(DeepNameAnalysis)
        self.output_mode_control = self._build_enum_segments(OutputMode)
        self.deep_name_analysis_control.selection_changed.connect(self._emit_deep_name_analysis)
        self.output_mode_control.selection_changed.connect(self._emit_output_mode)
        session_form.addRow(
            self._form_label("Deep name analysis", SEGMENTED_CONTROL_HEIGHT, self.deep_name_analysis_control),
            self.deep_name_analysis_control,
        )
        session_form.addRow(
            self._form_label("Output Mode", SEGMENTED_CONTROL_HEIGHT, self.output_mode_control),
            self.output_mode_control,
        )
        self.dp5_checkbox = QCheckBox("Run DP5q")
        self.dp5_checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dp5_checkbox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.dp5_checkbox.setToolTip(
            "Bundled DP5q availability is pending. Availability is checked for the first eligible compound."
        )
        self.dp5_checkbox.stateChanged.connect(self._emit_dp5_enabled)
        session_form.addRow(self._form_label("DP5q", FIELD_HEIGHT, self.dp5_checkbox), self.dp5_checkbox)
        session_layout.addLayout(session_form)
        self.options_help = QLabel()
        self.options_help.setWordWrap(True)
        session_layout.addWidget(self.options_help)
        layout.addWidget(session_card)

        controls_row = QHBoxLayout()
        controls_row.setContentsMargins(0, 0, 0, 0)
        controls_row.setSpacing(12)
        controls_row.addStretch(1)
        self.run_button = StandardButton("Run Analysis", role="primary")
        self.run_button.clicked.connect(self.run_requested)
        controls_row.addWidget(self.run_button)
        layout.addLayout(controls_row)

        self.analysis_card = QFrame()
        self.analysis_card.setProperty("card", "true")
        self.analysis_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        analysis_layout = QVBoxLayout(self.analysis_card)
        analysis_layout.setContentsMargins(16, 16, 16, 16)
        analysis_layout.setSpacing(14)


        self.progress_panel = QWidget()
        self.progress_panel.setProperty("cardSection", "true")
        self.progress_panel.setAutoFillBackground(False)
        progress_container = QVBoxLayout(self.progress_panel)
        progress_container.setContentsMargins(0, 0, 0, 0)
        progress_container.setSpacing(8)
        self.progress_bar = QProgressBar()
        self.progress_bar.setFormat("%v / %m")
        self.progress_bar.setTextVisible(False)
        self.progress_label = QLabel("")
        self.progress_label.setTextFormat(Qt.TextFormat.PlainText)
        self.progress_label.setWordWrap(True)
        self.progress_label.setProperty("cardSection", "true")
        self.progress_count_label = QLabel()
        self.progress_count_label.setAccessibleName("Current phase progress")
        self.progress_count_label.hide()
        progress_container.addWidget(self.progress_bar)
        progress_labels = QHBoxLayout()
        progress_labels.addWidget(self.progress_label, 1)
        progress_labels.addWidget(self.progress_count_label)
        progress_container.addLayout(progress_labels)
        self.progress_panel.setVisible(False)
        analysis_layout.addWidget(self.progress_panel)

        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setUndoRedoEnabled(False)
        self.log_output.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.log_output.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.log_output.setMinimumHeight(260)
        self.log_output.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log_output.setPlaceholderText("Run analysis to see progress and live messages here.")
        self.activity_details = CollapsibleSection("Activity details")
        self.activity_details.content_layout.addWidget(self.log_output)
        log_actions = QHBoxLayout()
        self.jump_to_latest_button = StandardButton("Jump to latest", role="secondary")
        self.jump_to_latest_button.clicked.connect(self._jump_to_latest)
        self.copy_details_button = StandardButton("Copy details", role="secondary")
        self.copy_details_button.clicked.connect(self._copy_details)
        log_actions.addWidget(self.jump_to_latest_button)
        log_actions.addWidget(self.copy_details_button)
        log_actions.addStretch(1)
        self.activity_details.content_layout.addLayout(log_actions)
        self.activity_details.expanded_changed.connect(self._activity_expanded)
        analysis_layout.addWidget(self.activity_details)
        layout.addWidget(self.analysis_card)

        self.summary_card = QFrame()
        self.summary_card.setProperty("card", "true")
        self.summary_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        summary_layout = QVBoxLayout(self.summary_card)
        summary_layout.setContentsMargins(16, 16, 16, 16)
        summary_layout.setSpacing(14)
        summary_title = QLabel("Run Summary")
        summary_title.setStyleSheet("font-size: 16px; font-weight: 600;")
        summary_layout.addWidget(summary_title)
        self.result_source_label = QLabel()
        self.result_source_label.setTextFormat(Qt.TextFormat.PlainText)
        self.result_source_label.setWordWrap(True)
        summary_layout.addWidget(self.result_source_label)

        badge_row = QHBoxLayout()
        badge_row.setContentsMargins(0, 0, 0, 0)
        badge_row.setSpacing(12)
        self.compounds_badge = SummaryBadge("Compounds", "0")
        self.critical_badge = SummaryBadge("Critical", "0", level="critical")
        self.warning_badge = SummaryBadge("Warnings", "0", level="warning")
        self.info_badge = SummaryBadge("Info", "0")
        self.status_badge = SummaryBadge("Status", "", level="warning")
        self.status_badge.setVisible(False)
        badge_row.addWidget(self.compounds_badge)
        badge_row.addWidget(self.critical_badge)
        badge_row.addWidget(self.warning_badge)
        badge_row.addWidget(self.info_badge)
        badge_row.addWidget(self.status_badge)
        badge_row.addStretch(1)
        summary_layout.addLayout(badge_row)

        assessment_row = QHBoxLayout()
        assessment_row.setSpacing(12)
        self.assessment_badge = SummaryBadge("Assessment")
        self.assessment_badge.setTextFormat(Qt.TextFormat.PlainText)
        self.assessment_counts = QLabel()
        self.assessment_counts.setTextFormat(Qt.TextFormat.PlainText)
        self.assessment_counts.setWordWrap(True)
        assessment_row.addWidget(self.assessment_badge)
        assessment_row.addWidget(self.assessment_counts, 1)
        summary_layout.addLayout(assessment_row)

        dp5_badge_row = QHBoxLayout()
        dp5_badge_row.setContentsMargins(0, 0, 0, 0)
        dp5_badge_row.setSpacing(12)
        self.dp5_n_badge = SummaryBadge("DP5q Assessed", "0")
        self.dp5_skipped_badge = SummaryBadge("DP5q Skipped", "0", level="warning")
        self.dp5_failed_badge = SummaryBadge("DP5q Failed", "0", level="critical")
        self.dp5_mean_badge = SummaryBadge("Mean DP5q score", "n/a")
        self._dp5_summary_badges = (
            self.dp5_n_badge,
            self.dp5_skipped_badge,
            self.dp5_failed_badge,
            self.dp5_mean_badge,
        )
        for badge in self._dp5_summary_badges:
            badge.setVisible(False)
            dp5_badge_row.addWidget(badge)
        dp5_badge_row.addStretch(1)
        summary_layout.addLayout(dp5_badge_row)

        self.summary_actions = QWidget()
        self.summary_actions.setProperty("cardSection", "true")
        self.summary_actions.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        action_grid = QHBoxLayout(self.summary_actions)
        action_grid.setContentsMargins(0, 0, 0, 0)
        self.open_html_button = StandardButton("Open Findings", role="primary")
        self.open_pdf_button = StandardButton("Open Findings (PDF)", role="secondary")
        self.open_results_button = StandardButton("Open Results (JSON)", role="secondary")
        self.open_source_pdf_button = StandardButton("Annotated Source (PDF)", role="secondary")
        self.open_error_stats_button = StandardButton("Statistics (XLSX)", role="secondary")
        self.open_output_folder_button = StandardButton("Open Output Folder", role="secondary")
        self.more_reports_button = StandardButton("Other formats", role="secondary")
        self.more_reports_button.setMinimumWidth(160)
        self.more_reports_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.more_reports_menu = QMenu(self.more_reports_button)
        self.more_reports_menu.setToolTipsVisible(True)
        self.more_reports_button.setMenu(self.more_reports_menu)
        for button in (
            self.open_pdf_button,
            self.open_results_button,
            self.open_source_pdf_button,
            self.open_error_stats_button,
        ):
            button.setParent(self)
            button.hide()
            action = self.more_reports_menu.addAction(button.text())
            action.triggered.connect(button.click)
            self._artifact_actions[button] = action
        action_grid.addWidget(self.open_html_button)
        action_grid.addWidget(self.open_output_folder_button)
        action_grid.addWidget(self.more_reports_button)
        action_grid.addStretch(1)
        summary_layout.addWidget(self.summary_actions)
        self.artifact_warning_label = QLabel()
        self.artifact_warning_label.setTextFormat(Qt.TextFormat.PlainText)
        self.artifact_warning_label.setWordWrap(True)
        self.artifact_warning_label.hide()
        summary_layout.addWidget(self.artifact_warning_label)
        layout.insertWidget(layout.indexOf(self.analysis_card), self.summary_card)
        layout.addStretch(1)
        scroll_area.setWidget(content)
        root_layout.addWidget(scroll_area, 1)
        self.clear_summary()
        self._refresh_option_help()

    def set_input_path(self, path: str) -> None:
        """Update the selected input path."""
        self.input_path_edit.setText(path)
        self.input_path_edit.setToolTip(path)
        self.input_title.setText(Path(path).name if Path(path).suffix.lower() in {".pdf", ".docx"} else "Choose a PDF or DOCX document")
        self.input_error_label.hide()
        if self._result_input_path and Path(path) != Path(self._result_input_path):
            self._result_is_previous = True
            self._render_result_source()

    @Slot(str)
    def set_output_directory(self, path: str) -> None:
        """Show the session's report parent without changing persisted settings."""
        self.output_dir_edit.setText(path)
        self.output_dir_edit.setToolTip(path)

    def input_path(self) -> str:
        """Return the dashboard's selected input path."""
        return self.input_path_edit.text().strip()

    def set_modes(self, config: AppConfig) -> None:
        """Sync the dashboard dropdowns from the session config."""
        self.deep_name_analysis_control.set_value(config.modes.deep_name_analysis)
        self.output_mode_control.set_value(config.modes.output_mode)
        self.set_dp5_enabled(config.dp5.enabled)
        self._refresh_option_help()

    def set_dp5_enabled(self, enabled: bool) -> None:
        """Sync the session DP5 toggle without emitting change signals."""
        blocked = self.dp5_checkbox.blockSignals(True)
        self.dp5_checkbox.setChecked(enabled)
        self.dp5_checkbox.blockSignals(blocked)
        self._refresh_option_help()

    def dp5_enabled(self) -> bool:
        """Return the current dashboard DP5 session toggle state."""
        return self.dp5_checkbox.isChecked()

    def set_dp5_runtime_status(self, valid: bool, messages: list[str]) -> None:
        """Show availability without changing the requested scoring setting."""
        if valid:
            self.dp5_checkbox.setToolTip("Run bundled DP5q compatibility scoring for this analysis.")
            return

        detail = messages[0] if messages else "Check bundled DP5q availability in Settings > DP5q > Refresh Status."
        self.dp5_checkbox.setToolTip(
            f"{detail} Availability is checked for the first eligible compound; disable scoring to run without DP5q."
        )

    def clear_log(self) -> None:
        """Reset the live log widget."""
        self.log_output.clear()

    def append_log(self, severity: str, message: str) -> None:
        """Follow new messages only while the reader is already at the bottom."""
        scrollbar = self.log_output.verticalScrollBar()
        position = scrollbar.value()
        following = position >= scrollbar.maximum() - 2 and not self.log_output.textCursor().hasSelection()
        char_format = QTextCharFormat()
        char_format.setForeground(QColor(ThemeController.log_color(severity)))

        cursor = QTextCursor(self.log_output.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        first_block = cursor.block()
        cursor.insertText(f"[{severity.title()}] {message.rstrip()}\n", char_format)
        block = first_block
        state = {"info": 0, "warning": 1, "critical": 2}.get(severity, 0)
        while block.isValid() and block.position() < cursor.position():
            block.setUserState(state)
            block = block.next()
        if following:
            scrollbar.setValue(scrollbar.maximum())
        else:
            scrollbar.setValue(position)

    def refresh_log_colors(self) -> None:
        """Recolor existing messages after an appearance change without rewriting them."""
        cursor = QTextCursor(self.log_output.document())
        block = self.log_output.document().firstBlock()
        levels = {0: "info", 1: "warning", 2: "critical"}
        cursor.beginEditBlock()
        try:
            while block.isValid():
                cursor.setPosition(block.position())
                cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
                formatting = QTextCharFormat()
                formatting.setForeground(QColor(ThemeController.log_color(levels.get(block.userState(), "info"))))
                cursor.mergeCharFormat(formatting)
                block = block.next()
        finally:
            cursor.endEditBlock()

    @Slot()
    def _jump_to_latest(self) -> None:
        scrollbar = self.log_output.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    @Slot()
    def _copy_details(self) -> None:
        if self.log_output.textCursor().hasSelection():
            self.log_output.copy()
        else:
            QApplication.clipboard().setText(self.log_output.toPlainText())

    @Slot(bool)
    def _activity_expanded(self, expanded: bool) -> None:
        if expanded:
            QTimer.singleShot(0, self, self._reveal_activity)

    @Slot()
    def _reveal_activity(self) -> None:
        self._content_layout.activate()
        self._scroll_area.ensureWidgetVisible(self.jump_to_latest_button, 0, 12)

    def set_progress(self, current: int, total: int, message: str) -> None:
        """Update progress feedback while a run is active."""
        if total <= 0:
            self.progress_bar.setRange(0, 0)
            self.progress_label.setText(message)
            self.progress_count_label.hide()
            return

        safe_total = max(total, 1)
        self.progress_bar.setRange(0, safe_total)
        self.progress_bar.setValue(min(max(current, 0), safe_total))
        self.progress_label.setText(message)
        self.progress_count_label.setText(f"{self.progress_bar.value()} / {safe_total}")
        self.progress_count_label.show()

    def reset_feedback(self) -> None:
        """Prepare the dashboard for a new run."""
        self.progress_bar.setRange(0, 1)
        self.progress_bar.reset()
        self.progress_label.clear()
        self.progress_count_label.hide()
        self.progress_panel.setVisible(False)

    def clear_summary(self) -> None:
        """Show an empty placeholder summary until a completed run exists."""
        self.compounds_badge.set_value("Compounds", "0")
        self.critical_badge.set_value("Critical", "0", level="critical")
        self.warning_badge.set_value("Warnings", "0", level="warning")
        self.info_badge.set_value("Info", "0")
        self.status_badge.setVisible(False)
        self.assessment_badge.setVisible(False)
        self.assessment_counts.setVisible(False)
        self._hide_dp5_summary()
        self._set_action_state(self.open_html_button, None)
        self._set_action_state(self.open_pdf_button, None)
        self._set_action_state(self.open_results_button, None)
        self._set_action_state(self.open_source_pdf_button, None)
        self._set_action_state(self.open_error_stats_button, None)
        self._set_action_state(self.open_output_folder_button, None)
        self.summary_card.setEnabled(False)
        self.summary_card.setVisible(False)
        self.summary_card.setToolTip("Run analysis once to populate this summary.")
        self._result_input_path = ""
        self._result_is_previous = False
        self._set_activity_compact(False)

    def show_summary(self, result: ApplicationRunResult, fallback_output_dir: str) -> None:
        """Render summary counts and enable artifact actions for the last run."""
        self._result_input_path = str(getattr(result, "input_path", ""))
        self._result_is_previous = False
        self._render_result_source()
        compounds = int(result.analysis_data.get("total_found", 0))
        self.compounds_badge.set_value("Compounds", str(compounds))
        critical = result.issue_counts.get("critical", 0)
        warnings_count = result.issue_counts.get("warning", 0)
        self.critical_badge.set_value("Critical", str(critical), level="critical" if critical else "default")
        self.warning_badge.set_value("Warnings", str(warnings_count), level="warning" if warnings_count else "default")
        self.info_badge.set_value("Info", str(result.issue_counts.get("info", 0)))

        assessment = result.analysis_data.get("assessment") or {}
        assessment_label = ASSESSMENT_LABELS.get(assessment.get("status"))
        self.assessment_badge.setVisible(bool(assessment_label))
        self.assessment_counts.setVisible(bool(assessment_label))
        if assessment_label:
            self.assessment_badge.set_value("Assessment", assessment_label,
                level="default" if assessment["status"] == "complete" else "warning")
            assessed = int(assessment.get("assessed_descriptions", 0))
            unassessed = len(assessment.get("unassessed", ()))
            descriptions = "description" if assessed == 1 else "descriptions"
            records = "record" if unassessed == 1 else "records"
            self.assessment_counts.setText(
                f"{assessed} {descriptions} assessed; {unassessed} unassessed content {records}. "
                + ("Open Findings for details." if result.report_data.get("html_path") or result.report_data.get("pdf_path") else ""))

        aborted = bool(result.analysis_data.get("aborted")) or bool(result.report_data.get("aborted"))
        partial = bool(result.report_data.get("partial"))
        self.status_badge.setVisible(aborted or partial)
        if aborted:
            self.status_badge.set_value("Status", "Cancelled", level="warning")
        elif partial:
            self.status_badge.set_value("Status", "Partial export", level="warning")

        self._render_dp5_summary(result.analysis_data.get("dp5_summary"))

        html_path = self._existing_path(result.report_data.get("html_path"))
        pdf_path = self._existing_path(result.report_data.get("pdf_path"))
        error_stats_path = self._existing_path(result.report_data.get("error_stats_path"))

        output_dir = (
            result.report_data.get("output_dir")
            or (html_path and str(Path(html_path).parent))
            or (pdf_path and str(Path(pdf_path).parent))
            or (error_stats_path and str(Path(error_stats_path).parent))
            or fallback_output_dir
        )

        self._set_action_state(self.open_html_button, html_path)
        self._set_action_state(self.open_pdf_button, pdf_path)
        self._set_action_state(self.open_results_button, self._existing_path(result.report_data.get("analysis_path")))
        self._set_action_state(self.open_source_pdf_button, self._existing_path(result.report_data.get("source_pdf_path")))
        self._set_action_state(self.open_error_stats_button, error_stats_path)
        self._set_action_state(self.open_output_folder_button, self._existing_path(output_dir, allow_directory=True))
        self.summary_card.setVisible(True)
        self.summary_card.setEnabled(True)
        warnings = "\n".join(str(item) for item in result.report_data.get("artifact_warnings", []))
        self.summary_card.setToolTip(warnings)
        self.artifact_warning_label.setText(warnings[:1200] + ("… Full text is available in this summary's tooltip." if len(warnings) > 1200 else ""))
        self.artifact_warning_label.setVisible(bool(warnings))
        if warnings:
            for button in (self.open_html_button, *self._artifact_actions):
                if not button.isEnabled():
                    button.setToolTip(f"Not generated for this run.\n{warnings}")
                    if action := self._artifact_actions.get(button):
                        action.setToolTip(button.toolTip())
        self.more_reports_button.setEnabled(any(action.isEnabled() for action in self._artifact_actions.values()))
        self._set_activity_compact(True)
        self.activity_details.set_expanded(False)
        QTimer.singleShot(0, self, self._reveal_summary)

    @Slot()
    def _reveal_summary(self) -> None:
        self._scroll_area.ensureWidgetVisible(self.summary_card, 0, 12)

    def _render_result_source(self) -> None:
        prefix = "Previous result" if self._result_is_previous else "Result"
        self.result_source_label.setText(
            f"{prefix} for {Path(self._result_input_path).name}" if self._result_input_path else "Last result"
        )
        self.result_source_label.setToolTip(self._result_input_path)

    def set_run_state(self, state: RunState) -> None:
        """Apply enabled/disabled dashboard control state."""
        self._run_state = state
        running = state is RunState.RUNNING
        if running:
            self._has_started_run = True
            self._result_is_previous = True
            self._render_result_source()
        self.run_button.setEnabled(not running)
        self.browse_button.setEnabled(not running)
        self.output_browse_button.setEnabled(not running)
        self.deep_name_analysis_control.setEnabled(not running)
        self.output_mode_control.setEnabled(not running)
        self.dp5_checkbox.setEnabled(not running)
        self.progress_panel.setVisible(running)
        if running:
            self._set_activity_compact(False)
        self._set_log_inactive(not self._has_started_run)
        self._refresh_option_help()

    @Slot(object)
    def _emit_deep_name_analysis(self, value: object) -> None:
        self._refresh_option_help()
        self.deep_name_analysis_changed.emit(value)

    @Slot(object)
    def _emit_output_mode(self, value: object) -> None:
        self._refresh_option_help()
        self.output_mode_changed.emit(value)

    @Slot(int)
    def _emit_dp5_enabled(self, _state: int) -> None:
        self._refresh_option_help()
        self.dp5_enabled_changed.emit(self.dp5_enabled())

    def _refresh_option_help(self) -> None:
        deep_analysis = self.deep_name_analysis_control.current_data()
        output = self.output_mode_control.current_data()
        scope = "Next run options" if self._run_state is RunState.RUNNING else "Run options"
        label = f"{scope} · Deep name analysis: {deep_analysis.value.title()} · {output.value.title()}"
        if self.dp5_enabled():
            label += " · DP5q on"
        self.options_section.toggle_button.setText(label)
        self.options_section.toggle_button.setAccessibleName(label)
        mode_help = ("Deep name analysis is off; checks use reported text without resolving names. "
            if deep_analysis is DeepNameAnalysis.OFF
            else "Deep name analysis resolves names with local OPSIN. ")
        output_help = "Detailed includes verbose diagnostics. " if output is OutputMode.DETAILED else "Essential keeps output concise. "
        self.options_help.setText(mode_help + output_help + "DP5q adds advisory scoring for eligible structures.")

    def _render_dp5_summary(self, summary: dict | None) -> None:
        if not summary:
            self._hide_dp5_summary()
            return
        self.dp5_n_badge.set_value("DP5q Assessed", str(summary.get("n", 0)))
        self.dp5_skipped_badge.set_value(
            "DP5q Skipped",
            str(summary.get("skipped", 0)),
            level="warning",
        )
        reasons = format_skip_reasons(summary.get("skipped_reasons"))
        self.dp5_skipped_badge.setToolTip(f"Skip reasons: {reasons}" if reasons else "")
        self.dp5_failed_badge.set_value(
            "DP5q Failed",
            str(summary.get("failed", 0)),
            level="critical",
        )
        mean = summary.get("mean_score_percent", summary.get("mean_probability_percent"))
        mean_text = f"{float(mean):.2f}%" if mean is not None else "n/a"
        self.dp5_mean_badge.set_value("Mean DP5q score", mean_text)
        for badge in self._dp5_summary_badges:
            badge.setVisible(True)

    def _hide_dp5_summary(self) -> None:
        for badge in self._dp5_summary_badges:
            badge.setVisible(False)

    @staticmethod
    def _build_enum_segments(enum_type) -> SingleSelectSegmentedControl:
        control = SingleSelectSegmentedControl()
        for member in enum_type:
            control.add_option(member.value.replace("_", " ").title(), member)
        if enum_type is DeepNameAnalysis:
            control.set_value(DeepNameAnalysis.OFF)
        return control

    @staticmethod
    def _form_label(text: str, minimum_height: int, control: QWidget | None = None) -> QLabel:
        label = QLabel(text)
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        label.setFixedHeight(minimum_height)
        if control is not None:
            label.setBuddy(control)
            if isinstance(control, SingleSelectSegmentedControl):
                control.set_field_name(text)
            else:
                control.setAccessibleName(text)
        return label

    def _set_log_inactive(self, inactive: bool) -> None:
        self.log_output.setProperty("inactive", "true" if inactive else "false")
        self.log_output.style().unpolish(self.log_output)
        self.log_output.style().polish(self.log_output)

    def _set_activity_compact(self, compact: bool) -> None:
        self.log_output.setMinimumHeight(160)
        self.log_output.setMaximumHeight(240 if compact else 420)
        self.analysis_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self.analysis_card.updateGeometry()

    @staticmethod
    def _existing_path(
        raw_path: str | None,
        *,
        allow_directory: bool = False,
    ) -> str | None:
        if not raw_path:
            return None
        candidate = Path(raw_path)
        if allow_directory and candidate.is_dir():
            return str(candidate)
        if candidate.is_file():
            return str(candidate)
        return None

    def _set_action_state(self, button: StandardButton, path: str | None) -> None:
        button.setEnabled(bool(path))
        button.setProperty("artifactPath", path or "")
        button.setToolTip(path or "Not available for this run")
        if action := self._artifact_actions.get(button):
            action.setEnabled(bool(path))
            action.setToolTip(button.toolTip())
