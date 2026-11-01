"""Main window and pages for the CheckSI desktop GUI."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QSize, QThread, QTimer, QUrl, Qt, Signal, Slot
from PySide6.QtGui import (
    QCloseEvent,
    QDesktopServices,
    QIcon,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from checksi.config import resolve_config_paths
from checksi.config_schema import AppConfig, DP5Settings, DeepNameAnalysis, OutputMode
from checksi.gui.config_editor import ConfigEditorPage
from checksi.gui.dashboard import DashboardPage, RunState
from checksi.gui.theme import ThemeController, ThemePreference
from checksi.gui.widgets import (
    NavigationRailButton,
    StandardButton,
    StandardComboBox,
    interface_icon as interface_icon,
)
from checksi.gui.worker import (
    AnalysisWorker,
    DP5StatusWorker,
    QtEventBridge,
    WorkerFailureKind,
)

APP_ICON_PATH = Path(__file__).resolve().parent / "assets" / "checksi_logo.png"


class AppSettingsPage(QWidget):
    """Theme and app-behavior settings that stay outside AppConfig."""

    theme_changed = Signal(str)

    def __init__(self, current_theme: ThemePreference, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("pageSurface")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        card = QFrame()
        card.setProperty("card", "true")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(12)

        title = QLabel("Appearance")
        title.setStyleSheet("font-size: 16px; font-weight: 600;")
        description = QLabel(
            "Choose a theme for this desktop app. Changes apply immediately and are saved "
            "automatically, separately from analytical settings."
        )
        description.setWordWrap(True)
        card_layout.addWidget(title)
        card_layout.addWidget(description)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.theme_combo = StandardComboBox()
        self.theme_combo.addItem("System Default", ThemePreference.SYSTEM)
        self.theme_combo.addItem("Light Theme", ThemePreference.LIGHT)
        self.theme_combo.addItem("Dark Theme", ThemePreference.DARK)
        self.theme_combo.refresh_contents_width()
        self.theme_combo.setAccessibleName("Application theme")
        form.addRow("&Theme", self.theme_combo)
        card_layout.addLayout(form)

        layout.addWidget(card)

        self.theme_combo.currentIndexChanged.connect(self._emit_theme_change)
        self.set_theme_preference(current_theme)

    def set_theme_preference(self, preference: ThemePreference) -> None:
        """Sync the page from the persisted theme preference."""
        for index in range(self.theme_combo.count()):
            if self.theme_combo.itemData(index) == preference:
                self.theme_combo.blockSignals(True)
                self.theme_combo.setCurrentIndex(index)
                self.theme_combo.blockSignals(False)
                return

    @Slot()
    def _emit_theme_change(self) -> None:
        current = ThemePreference(self.theme_combo.currentData())
        self.theme_changed.emit(current.value)


class CheckSIMainWindow(QMainWindow):
    """Top-level desktop shell for the CheckSI GUI."""

    def __init__(
        self,
        *,
        base_config: AppConfig,
        theme_controller: ThemeController,
    ) -> None:
        super().__init__()
        self._theme_controller = theme_controller
        self._run_state = RunState.IDLE
        self._shutdown_requested = False
        self._worker_thread: QThread | None = None
        self._worker: AnalysisWorker | None = None
        self._run_generation = 0
        self._run_terminal_state = RunState.IDLE
        self._dp5_status_thread: QThread | None = None
        self._dp5_status_worker: DP5StatusWorker | None = None
        self._dp5_status_generation = 0
        self._dp5_status_active_generation = 0
        self._dp5_status_settings: DP5Settings | None = None
        self._dp5_status_show_message = False
        self._dp5_status_refresh_pending = False
        self._dp5_status_pending_show_message = False
        self._dp5_status_full_check = False
        self._dp5_status_pending_full_check = False
        self._dp5_runtime_valid = False
        self._dp5_runtime_messages: list[str] = []
        self._cancel_token: threading.Event | None = None
        self._last_result = None
        self._last_run_config: AppConfig | None = None
        self._run_started_at: float | None = None

        self.setWindowTitle("CheckSI")
        self.setWindowIcon(QIcon(str(APP_ICON_PATH)))

        self._event_bridge: QtEventBridge | None = None

        self._build_ui()
        self._load_config_into_ui(base_config, preserve_dashboard_input=False)
        self._apply_window_geometry()

        self._theme_controller.follow_system_theme(self._handle_system_theme_change)

    def show_configuration_notices(self, notices: tuple[str, ...]) -> None:
        """Show startup migration notices in the same visible log as file loads."""
        for notice in notices:
            message = f"Configuration notice: {notice}"
            self.dashboard_page.append_log("info", message)
            self._show_status_message("info", message)

    def _build_ui(self) -> None:
        status_bar = QStatusBar()
        self.setStatusBar(status_bar)

        central = QWidget()
        central.setObjectName("appRoot")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        header = QFrame()
        header.setProperty("card", "true")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 8, 12, 8)
        header_layout.setSpacing(12)

        brand_title = QLabel("CheckSI")
        brand_title.setStyleSheet("font-size: 18px; font-weight: 700;")
        header_layout.addWidget(brand_title)
        header_layout.addStretch(1)

        self.dashboard_page = DashboardPage()
        self.config_editor_page = ConfigEditorPage()
        self.app_settings_page = AppSettingsPage(self._theme_controller.load_preference())
        self.config_editor_page.add_appearance_settings(self.app_settings_page)

        self.page_stack = QStackedWidget()
        self.page_stack.addWidget(self.dashboard_page)
        self.page_stack.addWidget(self.config_editor_page)

        button_group = QButtonGroup(self)
        button_group.setExclusive(True)
        self.analyze_nav_button = NavigationRailButton("Analyze", icon_name="analyze")
        self.settings_nav_button = NavigationRailButton("Settings", icon_name="settings")
        for page_index, button in enumerate((self.analyze_nav_button, self.settings_nav_button)):
            if page_index == 0:
                button.setChecked(True)
            button_group.addButton(button, page_index)
            header_layout.addWidget(button)

        button_group.idClicked.connect(self._navigate_to_page)

        layout.addWidget(header)
        self.run_feedback = QWidget()
        self.run_feedback.setProperty("transparentPanel", "true")
        feedback_layout = QHBoxLayout(self.run_feedback)
        feedback_layout.setContentsMargins(0, 0, 0, 0)
        self.run_status_label = QLabel("Ready")
        self.run_status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.run_status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.elapsed_label = QLabel()
        self.view_run_button = StandardButton("View check", role="secondary")
        self.view_run_button.clicked.connect(self.analyze_nav_button.click)
        self.global_cancel_button = StandardButton("Cancel", role="secondary")
        self.global_cancel_button.clicked.connect(self._cancel_analysis)
        feedback_layout.addWidget(self.run_status_label, 1)
        feedback_layout.addWidget(self.elapsed_label)
        feedback_layout.addWidget(self.view_run_button)
        feedback_layout.addWidget(self.global_cancel_button)
        self.run_feedback.hide()
        layout.addWidget(self.run_feedback)
        layout.addWidget(self.page_stack, 1)
        self.setCentralWidget(central)

        self.dashboard_page.browse_requested.connect(self._select_input_file)
        self.dashboard_page.output_browse_requested.connect(self._select_output_directory)
        self.dashboard_page.run_requested.connect(self._start_analysis)
        self.dashboard_page.deep_name_analysis_changed.connect(self._sync_editor_deep_name_analysis)
        self.dashboard_page.output_mode_changed.connect(self._sync_editor_output_mode)
        self.dashboard_page.dp5_enabled_changed.connect(self._sync_editor_dp5_enabled)
        self.config_editor_page.config_confirmed.connect(self._handle_settings_confirmed)
        self.config_editor_page.dp5_refresh_requested.connect(
            lambda: self._refresh_dp5_runtime_status(show_message=True)
        )
        self.config_editor_page.dp5_self_check_requested.connect(
            lambda: self._refresh_dp5_runtime_status(show_message=True, full_check=True)
        )

        self.dashboard_page.open_html_button.clicked.connect(
            lambda: self._open_summary_artifact("html_path")
        )
        self.dashboard_page.open_pdf_button.clicked.connect(
            lambda: self._open_summary_artifact("pdf_path")
        )
        self.dashboard_page.open_results_button.clicked.connect(
            lambda: self._open_summary_artifact("analysis_path")
        )
        self.dashboard_page.open_source_pdf_button.clicked.connect(
            lambda: self._open_summary_artifact("source_pdf_path")
        )
        self.dashboard_page.open_error_stats_button.clicked.connect(
            lambda: self._open_summary_artifact("error_stats_path")
        )
        self.dashboard_page.open_output_folder_button.clicked.connect(self._open_output_folder)

        self.config_editor_page.default_config_restored.connect(self._handle_default_config_restore)
        self.config_editor_page.config_loaded.connect(self._handle_editor_config_loaded)
        self.config_editor_page.status_message.connect(self._show_status_message)
        self.config_editor_page.migration_notice.connect(
            lambda notice: self.show_configuration_notices((notice,))
        )
        self.app_settings_page.theme_changed.connect(self._apply_theme_preference)
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._update_elapsed)
        self._open_shortcut = QShortcut(QKeySequence(QKeySequence.StandardKey.Open), self)
        self._open_shortcut.activated.connect(self._select_input_file)
        self.dashboard_page.browse_button.setToolTip(
            "Choose a document (" + self._open_shortcut.key().toString(QKeySequence.SequenceFormat.NativeText) + ")"
        )

    def _load_config_into_ui(
        self,
        config: AppConfig,
        *,
        preserve_dashboard_input: bool,
    ) -> None:
        if not preserve_dashboard_input:
            self.dashboard_page.set_input_path(str(config.paths.input_path))
        self.dashboard_page.set_modes(config)
        self.dashboard_page.set_output_directory(str(config.paths.output_dir))
        self.config_editor_page.load_from_config(config)
        self.dashboard_page.set_run_state(RunState.IDLE)
        self._refresh_dp5_runtime_status(show_message=False)
        self.dashboard_page.set_dp5_enabled(config.dp5.enabled)

    @Slot()
    def _select_input_file(self) -> None:
        if self._worker_thread is not None:
            return
        selected_path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Input File",
            self._documents_directory(self.dashboard_page.input_path()),
            "Documents (*.pdf *.docx);;PDF Files (*.pdf);;Word Documents (*.docx)",
        )
        if selected_path:
            self.dashboard_page.set_input_path(selected_path)
            self.analyze_nav_button.click()

    @Slot()
    def _select_output_directory(self) -> None:
        if self._worker_thread is not None:
            return
        selected = QFileDialog.getExistingDirectory(
            self, "Choose Output Parent Directory", self.dashboard_page.output_dir_edit.text()
        )
        if selected:
            self.config_editor_page.output_dir_edit.setText(selected)
            self.config_editor_page.confirm_changes()

    @Slot()
    def _start_analysis(self) -> None:
        if self._shutdown_requested:
            return
        if self._dp5_status_thread is not None and self._dp5_status_full_check:
            self.statusBar().showMessage("Wait for the DP5q full self-check to finish before starting analysis.", 5000)
            return
        if self._worker_thread is not None:
            self.statusBar().showMessage("Wait for the current analysis to finish.", 5000)
            return
        if not self.config_editor_page.request_confirmation():
            self.settings_nav_button.click()
            return
        try:
            run_config = self._build_run_config()
        except Exception as exc:  # noqa: BLE001 - user-facing validation
            if self.config_editor_page.focus_validation_error(exc):
                self.settings_nav_button.click()
                self.config_editor_page.focus_validation_error(exc)
            else:
                self.dashboard_page.input_error_label.setText(str(exc)[:600])
                self.dashboard_page.input_error_label.setToolTip(str(exc))
                self.dashboard_page.input_error_label.show()
            self._show_status_message("critical", str(exc))
            return

        self._last_run_config = run_config.model_copy(deep=True)
        self.dashboard_page.clear_log()
        self.dashboard_page.reset_feedback()
        self.dashboard_page.input_error_label.hide()
        self.dashboard_page.append_log("info", "Starting analysis...")
        self._run_started_at = time.monotonic()
        self._set_run_state(RunState.RUNNING)

        self._cancel_token = threading.Event()
        self._run_generation += 1
        self._run_terminal_state = RunState.IDLE
        self._event_bridge = QtEventBridge(self, operation_id=self._run_generation)
        self._event_bridge.log_received.connect(self._handle_log_event)
        self._event_bridge.progress_received.connect(self._handle_progress_event)
        self._worker_thread = QThread(self)
        self._worker = AnalysisWorker(
            config=run_config,
            input_path=str(run_config.paths.input_path),
            event_dispatcher=self._event_bridge.dispatch,
            cancel_token=self._cancel_token,
            operation_id=self._run_generation,
        )
        self._worker.moveToThread(self._worker_thread)

        self._worker_thread.started.connect(self._worker.run)
        self._worker.done.connect(self._worker_thread.quit)
        self._worker.succeeded.connect(self._handle_worker_success)
        self._worker.failed.connect(self._handle_worker_failure)
        self._worker_thread.finished.connect(self._handle_worker_finished)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.start()
        self.statusBar().clearMessage()

    def _build_run_config(self) -> AppConfig:
        self.config_editor_page.clear_validation_feedback()
        selected_input = self.dashboard_page.input_path()
        if not selected_input:
            raise ValueError("Select a PDF or DOCX file before running analysis.")

        input_path = Path(selected_input).expanduser()
        if not input_path.is_file():
            raise ValueError(f"Input file not found: {input_path}")
        if input_path.suffix.lower() not in {".pdf", ".docx"}:
            raise ValueError("Input file must be a PDF or DOCX document.")

        config = resolve_config_paths(self.config_editor_page.confirmed_config())
        config.paths.input_path = input_path
        config.dp5.enabled = self.dashboard_page.dp5_enabled()

        return config

    def _set_run_state(self, state: RunState) -> None:
        self._run_state = state
        self.dashboard_page.set_run_state(state)
        running = state is RunState.RUNNING
        self.config_editor_page.set_run_active(running)
        self.run_feedback.setVisible(running)
        self.global_cancel_button.setEnabled(running)
        self.global_cancel_button.setText("Cancel")
        if running:
            self.run_status_label.setText(f"{self._active_run_identity()} · Starting check")
            self._elapsed_timer.start()
        else:
            self._elapsed_timer.stop()
        self._update_elapsed()

    @Slot()
    def _update_elapsed(self) -> None:
        if self._run_started_at is not None:
            seconds = max(0, int(time.monotonic() - self._run_started_at))
            self.elapsed_label.setText(f"{seconds // 60}:{seconds % 60:02d}")

    def _active_run_identity(self) -> str:
        config = self._last_run_config
        if config is None:
            return Path(self.dashboard_page.input_path()).name
        return f"Deep name analysis: {config.modes.deep_name_analysis.value.title()} · {Path(config.paths.input_path).name}"

    @Slot(str, str)
    def _show_status_message(self, severity: str, message: str) -> None:
        timeout = 8000 if severity == "critical" else 5000
        self.statusBar().showMessage(message, timeout)

    @Slot(int, str, str)
    def _handle_log_event(self, operation_id: int, severity: str, message: str) -> None:
        if operation_id != self._run_generation or self._worker_thread is None:
            return
        self.dashboard_page.append_log(severity, message)
        if message.strip() and self._run_state is not RunState.RUNNING:
            self._show_status_message(severity, message.strip().splitlines()[-1])

    @Slot(int, int, int, str)
    def _handle_progress_event(self, operation_id: int, current: int, total: int, message: str) -> None:
        if operation_id != self._run_generation or self._worker_thread is None:
            return
        self.dashboard_page.set_progress(current, total, message)
        prefix = "Cancelling · " if self._cancel_token is not None and self._cancel_token.is_set() else ""
        text = f"{self._active_run_identity()} · {prefix}{message}"
        self.run_status_label.setText(text)
        self.run_status_label.setToolTip(text)

    @Slot(int, object)
    def _handle_worker_success(self, operation_id: int, result) -> None:
        if operation_id != self._run_generation or self._worker_thread is None:
            return
        self._last_result = result
        for warning in result.report_data.get("artifact_warnings", ()):
            self.dashboard_page.append_log("warning", str(warning))
        fallback_output_dir = (
            str(self._last_run_config.paths.output_dir)
            if self._last_run_config is not None
            else str(Path.cwd())
        )
        self.dashboard_page.show_summary(
            result,
            fallback_output_dir,
        )
        self._run_terminal_state = RunState.FINISHED
        if result.analysis_data.get("aborted") or result.report_data.get("aborted"):
            self.dashboard_page.set_progress(1, 1, "Cancelled")
            self.statusBar().showMessage("Analysis cancelled.", 5000)
        elif result.report_data.get("partial"):
            self.dashboard_page.set_progress(1, 1, "Partial export")
            self.statusBar().showMessage("Analysis finished with a partial export.", 5000)
        else:
            self.dashboard_page.set_progress(1, 1, "Completed")
            self.statusBar().showMessage("Analysis finished.", 5000)

    @Slot(int, object)
    def _handle_worker_failure(self, operation_id: int, failure) -> None:
        if operation_id != self._run_generation or self._worker_thread is None:
            return
        self.dashboard_page.append_log("critical", failure.message)
        self.dashboard_page.input_error_label.setText(f"Current check failed: {failure.message}"[:600])
        self.dashboard_page.input_error_label.setToolTip(failure.message)
        self.dashboard_page.input_error_label.show()
        if failure.details.strip():
            self.dashboard_page.append_log("critical", failure.details.strip())
        self._run_terminal_state = RunState.IDLE
        self.dashboard_page.set_progress(0, 1, "Failed")
        self.statusBar().showMessage("Analysis failed.", 5000)
        if failure.kind is WorkerFailureKind.DP5_PREFLIGHT:
            self._refresh_dp5_runtime_status(show_message=False)
        QMessageBox.critical(self, "Analysis Failed", failure.message)

    @Slot()
    def _handle_worker_finished(self) -> None:
        if self._worker_thread is None or self.sender() is not self._worker_thread:
            return
        # Join native teardown before releasing Python-owned worker references.
        self._worker_thread.wait()
        self._worker = None
        self._worker_thread = None
        self._cancel_token = None
        if self._event_bridge is not None:
            self._event_bridge.deleteLater()
            self._event_bridge = None
        self._set_run_state(self._run_terminal_state)
        if self._shutdown_requested:
            QTimer.singleShot(0, self.close)
        elif self._dp5_status_refresh_pending:
            show_message = self._dp5_status_pending_show_message
            self._dp5_status_refresh_pending = False
            self._dp5_status_pending_show_message = False
            self._refresh_dp5_runtime_status(show_message=show_message)

    @Slot()
    def _cancel_analysis(self) -> None:
        if self._cancel_token is None or self._cancel_token.is_set():
            return
        self._cancel_token.set()
        self.global_cancel_button.setText("Cancelling…")
        self.global_cancel_button.setEnabled(False)
        self.run_status_label.setText("Cancelling · waiting for the current step to stop safely")
        self.dashboard_page.append_log(
            "warning",
            "Cancellation requested. Waiting for the current step to stop cleanly...",
        )
        self.statusBar().showMessage("Cancelling analysis...")

    @Slot(object)
    def _sync_editor_deep_name_analysis(self, value) -> None:
        if isinstance(value, DeepNameAnalysis):
            self.config_editor_page.default_deep_name_analysis_control.set_value(value)
            self.config_editor_page.confirm_changes()

    @Slot(object)
    def _sync_editor_output_mode(self, value) -> None:
        if isinstance(value, OutputMode):
            self.config_editor_page.default_output_mode_control.set_value(value)
            self.config_editor_page.confirm_changes()

    @Slot(object)
    def _sync_editor_dp5_enabled(self, value) -> None:
        self.config_editor_page.set_dp5_enabled(bool(value))
        self.config_editor_page.confirm_changes()

    @Slot(object)
    def _handle_default_config_restore(self, config: AppConfig) -> None:
        self._refresh_dp5_runtime_status(show_message=False)
        self.statusBar().showMessage("Factory settings restored in the draft. Confirm Changes to apply them.", 5000)

    @Slot(object)
    def _handle_editor_config_loaded(self, config: AppConfig) -> None:
        self._refresh_dp5_runtime_status(show_message=False)

    @Slot(object)
    def _handle_settings_confirmed(self, config: AppConfig) -> None:
        self.dashboard_page.set_modes(config)
        self.dashboard_page.set_output_directory(str(config.paths.output_dir))
        self.dashboard_page.set_dp5_enabled(config.dp5.enabled)

    @Slot(int)
    def _navigate_to_page(self, page_index: int) -> None:
        if (page_index == 0 and self.page_stack.currentWidget() is self.config_editor_page
                and not self.config_editor_page.request_confirmation()):
            self.settings_nav_button.setChecked(True)
            return
        self.page_stack.setCurrentIndex(page_index)

    def _refresh_dp5_runtime_status(self, *, show_message: bool, full_check: bool = False) -> None:
        """Queue metadata or explicit full checks while another operation owns the worker."""
        if self._shutdown_requested:
            return
        self._dp5_status_generation += 1
        full_check = full_check or self._dp5_status_pending_full_check
        if self._dp5_status_thread is not None or self._worker_thread is not None:
            self._dp5_status_refresh_pending = True
            self._dp5_status_pending_show_message = (
                self._dp5_status_pending_show_message or show_message
            )
            self._dp5_status_pending_full_check = full_check or self._dp5_status_full_check
            if self._dp5_status_pending_full_check:
                self.config_editor_page.set_dp5_self_check_queued()
            return

        settings = self.config_editor_page.dp5_settings().model_copy(deep=True)
        self._dp5_status_settings = settings
        self._dp5_status_active_generation = self._dp5_status_generation
        self._dp5_status_show_message = show_message
        self._dp5_status_full_check = full_check
        self._dp5_status_pending_full_check = False
        self.config_editor_page.set_dp5_status_refreshing(True, full_check=full_check)

        self._dp5_status_thread = QThread(self)
        self._dp5_status_worker = DP5StatusWorker(
            settings=settings, operation_id=self._dp5_status_generation, full_check=full_check
        )
        self._dp5_status_worker.moveToThread(self._dp5_status_thread)
        self._dp5_status_thread.started.connect(self._dp5_status_worker.run)
        self._dp5_status_worker.succeeded.connect(self._handle_dp5_status_success)
        self._dp5_status_worker.failed.connect(self._handle_dp5_status_failure)
        self._dp5_status_worker.done.connect(self._dp5_status_thread.quit)
        self._dp5_status_thread.finished.connect(self._handle_dp5_status_finished)
        self._dp5_status_thread.finished.connect(self._dp5_status_thread.deleteLater)
        self._dp5_status_thread.start()

    def _dp5_status_is_current(self, operation_id: int) -> bool:
        """Reject advisory results for superseded settings or lifecycle owners."""
        if (
            self._dp5_status_thread is None
            or operation_id != self._dp5_status_active_generation
            or self._shutdown_requested
        ):
            return False
        current = (
            operation_id == self._dp5_status_generation
            and self._dp5_status_settings is not None
            and self._worker_thread is None
        )
        if not current:
            self._dp5_status_refresh_pending = True
            self._dp5_status_pending_full_check = (
                self._dp5_status_pending_full_check or self._dp5_status_full_check
            )
            self._dp5_status_pending_show_message = (
                self._dp5_status_pending_show_message or self._dp5_status_show_message
            )
        return current

    @Slot(int, object)
    def _handle_dp5_status_success(self, operation_id: int, result) -> None:
        if not self._dp5_status_is_current(operation_id):
            return
        ok, messages = result
        self._dp5_runtime_valid = ok
        self._dp5_runtime_messages = list(messages)
        self.config_editor_page.set_dp5_runtime_status(ok, messages, full_check=self._dp5_status_full_check)
        self.dashboard_page.set_dp5_runtime_status(ok, messages)
        if self._dp5_status_show_message:
            if self._dp5_status_full_check:
                self.statusBar().showMessage(
                    "DP5q full self-check passed." if ok else "DP5q full self-check failed. See Settings for diagnostics.", 8000
                )
            elif ok:
                self.statusBar().showMessage("Bundled DP5q is ready.", 5000)
            else:
                self.statusBar().showMessage("Bundled DP5q is unavailable.", 8000)

    @Slot(int, object)
    def _handle_dp5_status_failure(self, operation_id: int, failure) -> None:
        if not self._dp5_status_is_current(operation_id):
            return
        messages = [failure.message]
        self._dp5_runtime_valid = False
        self._dp5_runtime_messages = messages
        self.config_editor_page.set_dp5_runtime_status(False, messages, full_check=self._dp5_status_full_check)
        self.dashboard_page.set_dp5_runtime_status(False, messages)
        if failure.details.strip():
            self.dashboard_page.append_log("critical", failure.details.strip())
        if self._dp5_status_show_message:
            self.statusBar().showMessage(
                "DP5q full self-check failed." if self._dp5_status_full_check
                else "Bundled DP5q availability check failed.", 8000
            )

    @Slot()
    def _handle_dp5_status_finished(self) -> None:
        if self._dp5_status_thread is None or self.sender() is not self._dp5_status_thread:
            return
        self._dp5_status_thread.wait()
        self.config_editor_page.set_dp5_status_refreshing(False)
        self._dp5_status_worker = None
        self._dp5_status_thread = None
        self._dp5_status_settings = None
        self._dp5_status_full_check = False
        if self._shutdown_requested:
            self._dp5_status_refresh_pending = False
            self._dp5_status_pending_show_message = False
            self._dp5_status_pending_full_check = False
            QTimer.singleShot(0, self.close)
            return
        if self._worker_thread is not None:
            return
        show_message = self._dp5_status_pending_show_message
        should_refresh = self._dp5_status_refresh_pending
        self._dp5_status_refresh_pending = False
        self._dp5_status_pending_show_message = False
        if should_refresh:
            QTimer.singleShot(
                0,
                lambda show_message=show_message: self._refresh_dp5_runtime_status(
                    show_message=show_message
                ),
            )
            return

    @Slot(str)
    def _apply_theme_preference(self, value: str) -> None:
        preference = ThemePreference(value)
        applied = self._theme_controller.apply(preference)
        self.dashboard_page.refresh_log_colors()
        self.app_settings_page.set_theme_preference(applied)
        self.statusBar().showMessage(f"Applied {applied.value} theme preference.", 4000)

    @Slot()
    def _handle_system_theme_change(self, *_args) -> None:
        if self._theme_controller.load_preference() is ThemePreference.SYSTEM:
            self._theme_controller.apply(ThemePreference.SYSTEM)
            self.dashboard_page.refresh_log_colors()

    def _open_summary_artifact(self, key: str) -> None:
        if self._last_result is None:
            return
        path = self._last_result.report_data.get(key)
        if not path or not Path(path).is_file():
            QMessageBox.information(self, "Open File", "That artifact was not generated for this run.")
            return
        self._open_local_path(Path(path))

    @Slot()
    def _open_output_folder(self) -> None:
        target = self.dashboard_page.open_output_folder_button.property("artifactPath")
        if target and Path(target).is_dir():
            self._open_local_path(Path(target))

    def _open_local_path(self, path: Path) -> None:
        if QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve()))):
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Unable to Open File")
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setText(f"Your system could not open this location:\n{path}\n\nYou can copy the path or open its folder.")
        copy_button = dialog.addButton("Copy Path", QMessageBox.ButtonRole.ActionRole)
        folder_button = dialog.addButton("Open Folder", QMessageBox.ButtonRole.ActionRole)
        dialog.addButton(QMessageBox.StandardButton.Close)
        dialog.exec()
        if dialog.clickedButton() is copy_button:
            QApplication.clipboard().setText(str(path))
        elif dialog.clickedButton() is folder_button:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent.resolve())))

    @staticmethod
    def _documents_directory(current_path: str = "") -> str:
        if current_path:
            current = Path(current_path).expanduser()
            if current.exists():
                return str(current.parent if current.is_file() else current)
        documents = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation
        )
        if documents:
            return documents
        return str(Path.cwd())

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: D401
        if not self._shutdown_requested and not self.config_editor_page.request_close():
            self.settings_nav_button.click()
            event.ignore()
            return
        self.centralWidget().setEnabled(False)
        if self._worker_thread is not None:
            self._shutdown_requested = True
            self._cancel_analysis()
            self.statusBar().showMessage(
                "Closing after the running analysis stops cleanly..."
            )
            event.ignore()
            return
        if self._dp5_status_thread is not None:
            self._shutdown_requested = True
            self.statusBar().showMessage("Closing after DP5q status check finishes...")
            event.ignore()
            return
        self._shutdown_requested = True
        event.accept()

    def _apply_window_geometry(self) -> None:
        """Fit the desktop work area while leaving room for native window frames."""
        screen = self.screen()
        available = screen.availableGeometry().size() if screen is not None else QSize(1024, 700)
        usable = QSize(max(available.width() - 32, 1), max(available.height() - 64, 1))
        minimum = QSize(760, 500).boundedTo(usable)
        default = QSize(1120, 760).boundedTo(usable).expandedTo(minimum)
        self.setMinimumSize(minimum)
        self.resize(default)
