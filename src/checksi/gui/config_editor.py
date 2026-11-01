"""Configuration editor page for AppConfig-backed settings."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QAbstractItemModel, QModelIndex, QSignalBlocker, QSize, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemDelegate,
    QAbstractItemView,
    QAbstractSpinBox,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionComboBox,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import Shiboken

from checksi.config import ConfigConflictError, ConfigError, ConfigManager, resolve_config_paths, user_config_path
from checksi.config_schema import (
    AppConfig,
    DP5Settings,
    ErrorSeverity,
    DeepNameAnalysis,
    OutputMode,
)
from checksi.errors.defs import AnalysisErrorCode
from checksi.errors.registry import ERROR_TEMPLATES
from checksi.gui.theme import ThemeController
from checksi.gui.widgets import (
    CollapsibleSection,
    FIELD_HEIGHT,
    SEGMENTED_CONTROL_HEIGHT,
    SingleSelectSegmentedControl,
    StandardButton,
    StandardComboBox,
    StandardLineEdit,
)


class AdvancedConfigDialog(QDialog):
    """Inspect factory JSON and validate exact settings before applying to a session."""

    def __init__(self, config: AppConfig, factory: AppConfig, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Advanced Configuration")
        self.resize(760, 600)
        self.validated_config: AppConfig | None = None
        self.migration_notices: tuple[str, ...] = ()
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Edit the complete session configuration as JSON. Apply changes only to this "
            "draft; use Confirm Changes in Settings to apply it, or Save User Defaults to keep it after restarting. "
            "Factory defaults are read-only and do not replace your session."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.tabs = QTabWidget()
        self.session_edit = QPlainTextEdit(json.dumps(config.model_dump(mode="json"), indent=2, sort_keys=True))
        self.session_edit.setAccessibleName("Session configuration JSON")
        self.factory_edit = QPlainTextEdit(json.dumps(factory.model_dump(mode="json"), indent=2, sort_keys=True))
        self.factory_edit.setAccessibleName("Read-only factory configuration JSON")
        self.factory_edit.setReadOnly(True)
        for editor in (self.session_edit, self.factory_edit):
            editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            editor.setTabChangesFocus(True)
        self.tabs.addTab(self.session_edit, "Session configuration")
        self.tabs.addTab(self.factory_edit, "Factory defaults (read-only)")
        layout.addWidget(self.tabs, 1)
        self.validation_label = QLabel("")
        self.validation_label.setTextFormat(Qt.TextFormat.PlainText)
        self.validation_label.setWordWrap(True)
        self.validation_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.validation_scroll = QScrollArea()
        self.validation_scroll.setWidgetResizable(True)
        self.validation_scroll.setMaximumHeight(120)
        self.validation_scroll.setWidget(self.validation_label)
        self.validation_scroll.hide()
        layout.addWidget(self.validation_scroll)
        actions = QHBoxLayout()
        copy_button = StandardButton("Copy JSON", role="secondary")
        copy_button.clicked.connect(self._copy_current)
        actions.addWidget(copy_button)
        actions.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel)
        apply_button = buttons.button(QDialogButtonBox.StandardButton.Apply)
        apply_button.setText("Update Settings")
        apply_button.clicked.connect(self._apply_session)
        buttons.rejected.connect(self.reject)
        self.tabs.currentChanged.connect(lambda index: apply_button.setEnabled(index == 0))
        actions.addWidget(buttons)
        layout.addLayout(actions)

    @Slot()
    def _copy_current(self) -> None:
        editor = self.tabs.currentWidget()
        QApplication.clipboard().setText(editor.toPlainText())

    @Slot()
    def _apply_session(self) -> None:
        notices: list[str] = []
        try:
            config = AppConfig.model_validate_json(
                self.session_edit.toPlainText(), context={"migration_notices": notices}
            )
        except ValueError as exc:
            self.validation_label.setText(f"Configuration was not applied. {exc}")
            self.validation_scroll.show()
            self.session_edit.setFocus()
            return
        self.validated_config = config
        self.migration_notices = tuple(notices)
        self.accept()


class SolventEditorDialog(QDialog):
    """Modal form used when adding a single solvent entry."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Solvent")
        self.setModal(True)
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        intro = QLabel(
            "Enter the values for the new solvent. Use commas between aliases or shifts."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.name_edit = StandardLineEdit()
        self.aliases_edit = StandardLineEdit()
        self.proton_shifts_edit = StandardLineEdit()
        self.carbon_shifts_edit = StandardLineEdit()
        self.name_edit.setPlaceholderText("e.g. CDCl3")
        self.aliases_edit.setPlaceholderText("e.g. chloroform, deuterated chloroform")
        self.proton_shifts_edit.setPlaceholderText("e.g. 7.28")
        self.carbon_shifts_edit.setPlaceholderText("e.g. 77.0")

        form.addRow("Name", self.name_edit)
        form.addRow("Aliases", self.aliases_edit)
        form.addRow("Proton Shifts", self.proton_shifts_edit)
        form.addRow("Carbon Shifts", self.carbon_shifts_edit)
        layout.addLayout(form)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def values(self) -> tuple[str, str, str, str]:
        """Return the dialog field values as table-ready strings."""
        return (
            self.name_edit.text().strip(),
            self.aliases_edit.text().strip(),
            self.proton_shifts_edit.text().strip(),
            self.carbon_shifts_edit.text().strip(),
        )


class ErrorSeverityDelegate(QStyledItemDelegate):
    """Paint dropdowns and allocate a real editor only for the active cell."""

    def __init__(self, parent: QTableWidget) -> None:
        super().__init__(parent)
        self._display = StandardComboBox(parent)
        self._display.hide()

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        box = QStyleOptionComboBox()
        box.initFrom(self._display)
        margin = max((option.rect.height() - FIELD_HEIGHT) // 2, 0)
        box.rect = option.rect.adjusted(0, margin, 0, -margin)
        box.state = option.state
        box.currentText = index.data(Qt.ItemDataRole.DisplayRole)
        style = self._display.style()
        style.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, box, painter, self._display)
        text_rect = style.subControlRect(QStyle.ComplexControl.CC_ComboBox, box,
                                        QStyle.SubControl.SC_ComboBoxEditField, self._display)
        colors = ThemeController.colors(bool(QApplication.instance().property("themeDark")))
        color = colors["muted"] if index.data(Qt.ItemDataRole.UserRole) is None else colors["on_surface"]
        painter.save()
        painter.setFont(option.font)
        painter.setPen(QColor(color))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, box.currentText)
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        size = super().sizeHint(option, index)
        size.setWidth(max(size.width(), 190))
        return size

    def createEditor(self, parent: QWidget, option: QStyleOptionViewItem, index: QModelIndex) -> StandardComboBox:
        editor = ConfigEditorPage._build_error_override_combo()
        # The table owns its geometry; avoid Qt's delayed adjustSize().
        editor.setSizeAdjustPolicy(StandardComboBox.SizeAdjustPolicy.AdjustToContentsOnFirstShow)
        editor.setParent(parent)
        editor.setObjectName("errorSeverityEditor")
        editor.setAccessibleName(f"{index.sibling(index.row(), 0).data()} severity override")
        editor.activated.connect(lambda _index: self._finish_editing(editor))

        def open_popup_if_active() -> None:
            if Shiboken.isValid(editor) and editor.isVisible():
                editor.showPopup()

        # Qt flushes editor-addressed calls during close, before hiding the widget.
        QTimer.singleShot(0, self, open_popup_if_active)
        return editor

    def setEditorData(self, editor: StandardComboBox, index: QModelIndex) -> None:
        value = index.data(Qt.ItemDataRole.UserRole)
        ConfigEditorPage._set_combo_value(editor, value)
        editor.setProperty("usingDefault", "true" if value is None else "false")

    def setModelData(self, editor: StandardComboBox, model: QAbstractItemModel, index: QModelIndex) -> None:
        model.setData(index, editor.currentData(), Qt.ItemDataRole.UserRole)

    def updateEditorGeometry(self, editor: QWidget, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        margin = max((option.rect.height() - FIELD_HEIGHT) // 2, 0)
        editor.setGeometry(option.rect.adjusted(0, margin, 0, -margin))

    def _finish_editing(self, editor: StandardComboBox) -> None:
        self.commitData.emit(editor)
        self.closeEditor.emit(editor, QAbstractItemDelegate.EndEditHint.NoHint)


class ConfigEditorPage(QWidget):
    """Scrollable editor for loading, editing, and persisting AppConfig."""

    default_config_restored = Signal(object)
    config_loaded = Signal(object)
    status_message = Signal(str, str)
    migration_notice = Signal(str)
    dp5_refresh_requested = Signal()
    dp5_self_check_requested = Signal()
    config_confirmed = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("pageSurface")
        self._source_manager: ConfigManager | None = None
        self._dp5_runtime_valid = False
        # Loaded DP5q values the controls cannot show exactly; each is kept
        # until the user changes that control.
        self._dp5_preserved: dict[str, float | int] = {}
        self._margin_preserved: dict[str, float] = {}
        self._loaded_text_values: dict[QLineEdit, tuple[str, str | None]] = {}
        self._dp5_runtime_messages: list[str] = []
        self._dp5_details_visible = False
        self._dp5_detail_lines: list[str] = []
        self._loading = True
        self._confirmed_config: AppConfig | None = None
        self._confirmed_state = None
        self._saved_state = None
        self._run_active = False
        self._factory_overrides = ConfigManager.load_factory().errors.overrides
        self._build_ui()
        self.validation_error_label = QLabel()
        self.validation_error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.validation_error_label.setWordWrap(True)
        self.validation_error_label.hide()
        self.layout().insertWidget(1, self.validation_error_label)
        for control in (self.input_path_edit, self.output_dir_edit, self.libreoffice_path_edit):
            control.textChanged.connect(
                lambda _text, control=control: self._loaded_text_values.pop(control, None)
            )
        for control in self.findChildren(QLineEdit):
            if control is not self.error_search_edit:
                control.textChanged.connect(self._refresh_change_state)
        for control in self.findChildren(QCheckBox):
            if control is not self.changed_errors_checkbox:
                control.toggled.connect(self._refresh_change_state)
        for control in self.findChildren(SingleSelectSegmentedControl):
            control.selection_changed.connect(self._refresh_change_state)
        for control in self.findChildren(QAbstractSpinBox):
            control.valueChanged.connect(self._refresh_change_state)
        for control in self.findChildren(QSlider):
            control.valueChanged.connect(self._refresh_change_state)
        self.solvents_table.itemChanged.connect(self._refresh_change_state)
        self.solvents_table.model().rowsInserted.connect(self._refresh_change_state)
        self.solvents_table.model().rowsRemoved.connect(self._refresh_change_state)
        self._loading = False

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(16)

        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(0, 0, 0, 0)
        top_bar.setSpacing(12)
        self.restore_default_button = StandardButton("Restore Factory Settings", role="secondary")
        self.save_default_button = StandardButton("Save User Defaults", role="secondary")
        self.load_button = StandardButton("Import Config", role="secondary")
        self.save_config_button = StandardButton("Export Config", role="secondary")
        top_bar.addWidget(self.restore_default_button)
        top_bar.addWidget(self.save_default_button)
        top_bar.addWidget(self.load_button)
        top_bar.addWidget(self.save_config_button)
        top_bar.addStretch(1)
        root_layout.addLayout(top_bar)

        self.config_source_label = QLabel("Source: startup session configuration")
        self.config_source_label.setTextFormat(Qt.TextFormat.PlainText)
        self.config_source_label.setWordWrap(True)
        self.config_source_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root_layout.addWidget(self.config_source_label)
        session_row = QHBoxLayout()
        self.config_persistence_label = QLabel(
            "Confirm Changes applies edits to this session. Save User Defaults keeps settings after restarting."
        )
        self.config_persistence_label.setWordWrap(True)
        self.config_persistence_label.setTextFormat(Qt.TextFormat.PlainText)
        session_row.addWidget(self.config_persistence_label, 1)
        self.confirm_button = StandardButton("Confirm Changes", role="primary")
        self.confirm_button.setEnabled(False)
        self.confirm_button.clicked.connect(self.confirm_changes)
        session_row.addWidget(self.confirm_button)
        self.advanced_config_button = StandardButton("Advanced Configuration", role="secondary")
        self.advanced_config_button.clicked.connect(self._show_advanced_configuration)
        session_row.addWidget(self.advanced_config_button)
        root_layout.addLayout(session_row)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        self.content_layout = content_layout
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(16)

        paths_card, paths_layout = self._create_card(
            "Paths",
            "Edit the baseline input and output locations used when a session starts.",
        )
        self.input_path_edit = StandardLineEdit()
        self.input_path_edit.setPlaceholderText("Keep the last confirmed default input")
        self.input_path_edit.setToolTip(
            "Leave blank to keep the last confirmed default input. Analyze uses its selected document."
        )
        self.output_dir_edit = StandardLineEdit()
        paths_form = QFormLayout()
        paths_form.setContentsMargins(0, 0, 0, 0)
        paths_form.setSpacing(12)
        paths_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        paths_layout.addLayout(paths_form)
        self._add_path_row(
            paths_form,
            "Default Input",
            self.input_path_edit,
            self._browse_default_input,
        )
        self._add_path_row(
            paths_form,
            "Output Parent Directory",
            self.output_dir_edit,
            self._browse_output_directory,
        )
        content_layout.addWidget(paths_card)

        conversion_card, conversion_layout = self._create_card(
            "Document Conversion",
            "LibreOffice is optional. An explicit executable path overrides discovery on PATH.",
        )
        self.libreoffice_path_edit = StandardLineEdit()
        self.libreoffice_path_edit.setPlaceholderText("Discover LibreOffice on PATH")
        self.conversion_timeout_spinbox = QSpinBox()
        self.conversion_timeout_spinbox.setProperty("settingSpinBox", "true")
        self.conversion_timeout_spinbox.setRange(1, 3600)
        self.conversion_timeout_spinbox.setSuffix(" s")
        conversion_form = QFormLayout()
        conversion_form.addRow("LibreOffice Executable", self.libreoffice_path_edit)
        conversion_form.addRow("Conversion Timeout", self.conversion_timeout_spinbox)
        conversion_layout.addLayout(conversion_form)
        self.conversion_section = self._collapsed_card("Document Conversion", conversion_card)

        modes_card, modes_layout = self._create_card(
            "Modes",
            "Set the default analytical behavior, PDF extraction margins, and post-run file opening preference.",
        )
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.default_deep_name_analysis_control = self._build_enum_segments(DeepNameAnalysis)
        self.default_output_mode_control = self._build_enum_segments(OutputMode)
        self.open_reports_checkbox = QCheckBox("Open generated reports automatically on success")
        self.open_reports_checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_reports_checkbox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        form.addRow(
            self._form_label("Deep name analysis", SEGMENTED_CONTROL_HEIGHT, self.default_deep_name_analysis_control),
            self.default_deep_name_analysis_control,
        )
        form.addRow(
            self._form_label("Output Mode", SEGMENTED_CONTROL_HEIGHT, self.default_output_mode_control),
            self.default_output_mode_control,
        )
        form.addRow(self.open_reports_checkbox)
        (
            self.pdf_header_margin_controls,
            self.pdf_header_margin_slider,
            self.pdf_header_margin_spinbox,
        ) = self._build_percentage_control()
        (
            self.pdf_footer_margin_controls,
            self.pdf_footer_margin_slider,
            self.pdf_footer_margin_spinbox,
        ) = self._build_percentage_control()
        form.addRow(
            self._form_label("PDF Header Margin", FIELD_HEIGHT, self.pdf_header_margin_spinbox),
            self.pdf_header_margin_controls,
        )
        form.addRow(
            self._form_label("PDF Footer Margin", FIELD_HEIGHT, self.pdf_footer_margin_spinbox),
            self.pdf_footer_margin_controls,
        )
        modes_layout.addLayout(form)
        content_layout.addWidget(modes_card)

        dp5_card, dp5_layout = self._create_card(
            "DP5q",
            "Configure optional bundled DP5q compatibility scoring. Scores are advisory.",
        )
        self.dp5_card = dp5_card
        self.dp5_status_label = QLabel("Status unknown")
        self.dp5_status_label.setStyleSheet("font-weight: 600;")
        self.dp5_status_message_label = QLabel("Refresh status to check the bundled model metadata.")
        self.dp5_status_message_label.setWordWrap(True)
        self.dp5_status_message_label.setTextFormat(Qt.TextFormat.PlainText)
        dp5_layout.addWidget(self.dp5_status_label)
        dp5_layout.addWidget(self.dp5_status_message_label)

        dp5_actions = QHBoxLayout()
        dp5_actions.setContentsMargins(0, 0, 0, 0)
        dp5_actions.setSpacing(12)
        self.dp5_refresh_button = StandardButton("Refresh Status", role="secondary")
        self.dp5_self_check_button = StandardButton("Run Full Self-check", role="secondary")
        self.dp5_details_button = StandardButton("Show Details", role="secondary")
        dp5_actions.addWidget(self.dp5_refresh_button)
        dp5_actions.addWidget(self.dp5_self_check_button)
        dp5_actions.addWidget(self.dp5_details_button)
        dp5_actions.addStretch(1)
        dp5_layout.addLayout(dp5_actions)

        self.dp5_details_panel = QWidget()
        self.dp5_details_panel.setProperty("detailsPanel", "true")
        dp5_details_layout = QVBoxLayout(self.dp5_details_panel)
        dp5_details_layout.setContentsMargins(0, 0, 0, 0)
        dp5_details_layout.setSpacing(0)
        self.dp5_details_label = QLabel("")
        self.dp5_details_label.setProperty("detailsText", "true")
        self.dp5_details_label.setTextFormat(Qt.TextFormat.PlainText)
        self.dp5_details_label.setWordWrap(True)
        self.dp5_details_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        dp5_details_layout.addWidget(self.dp5_details_label)
        self.dp5_details_panel.setVisible(False)
        dp5_layout.addWidget(self.dp5_details_panel)

        self.dp5_settings_panel = QWidget()
        self.dp5_settings_panel.setProperty("transparentPanel", "true")
        self.dp5_settings_panel.setAutoFillBackground(False)
        dp5_settings_form = QFormLayout(self.dp5_settings_panel)
        dp5_settings_form.setContentsMargins(0, 0, 0, 0)
        dp5_settings_form.setSpacing(12)
        dp5_settings_form.setLabelAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        dp5_settings_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        self.dp5_enabled_checkbox = QCheckBox("Enable DP5q by default")
        self.dp5_enabled_checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dp5_enabled_checkbox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        (
            self.dp5_threshold_controls,
            self.dp5_threshold_slider,
            self.dp5_threshold_spinbox,
        ) = self._build_numeric_slider_control(
            minimum=0.0,
            maximum=100.0,
            step=0.5,
            decimals=1,
            suffix="%",
            width_sample="100.0%",
        )
        (
            self.dp5_timeout_controls,
            self.dp5_timeout_slider,
            self.dp5_timeout_spinbox,
        ) = self._build_numeric_slider_control(
            minimum=30,
            maximum=1800,
            step=30,
            decimals=0,
            suffix=" s",
            width_sample="1800 s",
        )
        for name, spinbox, slider in (
            ("score_threshold_percent", self.dp5_threshold_spinbox, self.dp5_threshold_slider),
            ("timeout_seconds", self.dp5_timeout_spinbox, self.dp5_timeout_slider),
        ):
            # Slider drags update the spin box with its signals blocked, so
            # both controls report user edits.
            spinbox.valueChanged.connect(lambda _value, name=name: self._forget_preserved_dp5_value(name))
            slider.valueChanged.connect(lambda _value, name=name: self._forget_preserved_dp5_value(name))
        self.dp5_keep_artifacts_checkbox = QCheckBox("Keep DP5q JSON and geometry artifacts in the output folder")
        self.dp5_keep_artifacts_checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dp5_keep_artifacts_checkbox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        dp5_layout.addWidget(self.dp5_enabled_checkbox)
        dp5_settings_form.addRow(
            self._form_label("Low-score Threshold", FIELD_HEIGHT, self.dp5_threshold_spinbox),
            self.dp5_threshold_controls,
        )
        dp5_settings_form.addRow(
            self._form_label("Timeout", FIELD_HEIGHT, self.dp5_timeout_spinbox),
            self.dp5_timeout_controls,
        )
        dp5_settings_form.addRow(self.dp5_keep_artifacts_checkbox)
        self.dp5_tuning_section = CollapsibleSection("Detailed DP5q Settings")
        self.dp5_tuning_section.content_layout.addWidget(self.dp5_settings_panel)
        dp5_layout.addWidget(self.dp5_tuning_section)
        content_layout.addWidget(dp5_card)
        content_layout.addWidget(self.conversion_section)

        errors_card, errors_layout = self._create_card(
            "Errors",
            "Choose the default severity and optionally expose per-code overrides only where needed.",
        )
        error_header_layout = QHBoxLayout()
        error_header_layout.setContentsMargins(0, 0, 0, 0)
        error_header_layout.setSpacing(12)
        self.default_severity_control = self._build_enum_segments(ErrorSeverity)
        error_header_layout.addWidget(
            self._form_label("Default Severity", SEGMENTED_CONTROL_HEIGHT, self.default_severity_control)
        )
        error_header_layout.addWidget(self.default_severity_control, 1)
        errors_layout.addLayout(error_header_layout)

        error_actions = QHBoxLayout()
        error_actions.setContentsMargins(0, 0, 0, 0)
        error_actions.setSpacing(12)
        self.toggle_overrides_button = StandardButton(
            "Show Per-Error Overrides",
            role="secondary",
        )
        self.reset_overrides_button = StandardButton(
            "Reset Everything to Default Severity",
            role="secondary",
        )
        self.reset_overrides_button.setEnabled(False)
        error_actions.addWidget(self.toggle_overrides_button)
        error_actions.addWidget(self.reset_overrides_button)
        error_actions.addStretch(1)
        errors_layout.addLayout(error_actions)

        self.error_overrides_panel = QWidget()
        self.error_overrides_panel.setVisible(False)
        overrides_layout = QVBoxLayout(self.error_overrides_panel)
        overrides_layout.setContentsMargins(0, 0, 0, 0)
        overrides_layout.setSpacing(12)
        overrides_intro = QLabel(
            "Only rows changed here override the default severity above. "
            "Rows left as \"Use Default\" stay tied to the default setting."
        )
        overrides_intro.setWordWrap(True)
        overrides_layout.addWidget(overrides_intro)

        filter_row = QHBoxLayout()
        self.error_search_edit = StandardLineEdit()
        self.error_search_edit.setPlaceholderText("Search error codes and text…")
        self.error_search_edit.setAccessibleName("Search error codes and text")
        self.error_search_edit.setClearButtonEnabled(True)
        self.changed_errors_checkbox = QCheckBox("Show only changed")
        self.changed_errors_checkbox.setToolTip("Show severity overrides that differ from factory settings.")
        self.changed_errors_checkbox.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        filter_row.addWidget(self.error_search_edit, 1)
        filter_row.addWidget(self.changed_errors_checkbox)
        overrides_layout.addLayout(filter_row)
        self.error_filter_status = QLabel()
        overrides_layout.addWidget(self.error_filter_status)
        self.error_search_edit.textChanged.connect(self._filter_error_rows)
        self.changed_errors_checkbox.toggled.connect(self._filter_error_rows)

        self.errors_table = QTableWidget(0, 2)
        self.errors_table.setHorizontalHeaderLabels(["Error Code", "Override Severity"])
        self.errors_table.verticalHeader().setVisible(False)
        self.errors_table.setEditTriggers(QAbstractItemView.EditTrigger.EditKeyPressed |
                                         QAbstractItemView.EditTrigger.DoubleClicked)
        self.errors_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.errors_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.errors_table.verticalHeader().setDefaultSectionSize(48)
        self.errors_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.errors_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.errors_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding)
        self.errors_table.setMinimumHeight(360)
        self._initialize_error_override_rows()
        self.errors_table.setItemDelegateForColumn(1, ErrorSeverityDelegate(self.errors_table))
        self.errors_table.itemChanged.connect(self._error_override_edited)
        self.errors_table.clicked.connect(self._edit_error_override)
        self.errors_table.activated.connect(self._edit_error_override)
        overrides_layout.addWidget(self.errors_table)
        errors_layout.addWidget(self.error_overrides_panel)
        self.errors_section = self._collapsed_card("Diagnostic Settings", errors_card)
        content_layout.addWidget(self.errors_section)

        solvents_card, solvents_layout = self._create_card(
            "Solvents",
            "Edit solvent aliases and expected residual 1H/13C shifts used during parsing.",
        )
        solvent_actions = QHBoxLayout()
        solvent_actions.setContentsMargins(0, 0, 0, 0)
        solvent_actions.setSpacing(12)
        self.add_solvent_button = StandardButton("Add Solvent", role="secondary")
        self.remove_solvent_button = StandardButton("Remove Selected", role="secondary")
        solvent_actions.addWidget(self.add_solvent_button)
        solvent_actions.addWidget(self.remove_solvent_button)
        solvent_actions.addStretch(1)
        solvents_layout.addLayout(solvent_actions)

        self.solvents_table = QTableWidget(0, 4)
        self.solvents_table.setHorizontalHeaderLabels(
            ["Name", "Aliases", "Proton Shifts", "Carbon Shifts"]
        )
        self.solvents_table.verticalHeader().setVisible(False)
        self.solvents_table.verticalHeader().setDefaultSectionSize(42)
        self.solvents_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.solvents_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.solvents_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.solvents_table.setMinimumHeight(280)
        self.solvents_table.itemChanged.connect(self._forget_preserved_solvent_value)
        solvents_layout.addWidget(self.solvents_table)
        self.solvents_section = self._collapsed_card("Solvent Settings", solvents_card)
        content_layout.addWidget(self.solvents_section)

        content_layout.addStretch(1)
        scroll_area.setWidget(content)
        root_layout.addWidget(scroll_area, 1)

        self.load_button.clicked.connect(self._load_config_from_file)
        self.save_config_button.clicked.connect(self._save_config)
        self.add_solvent_button.clicked.connect(self._show_add_solvent_dialog)
        self.remove_solvent_button.clicked.connect(self._remove_selected_solvent)
        self.restore_default_button.clicked.connect(self._restore_default_config)
        self.save_default_button.clicked.connect(self._save_default_config)
        self.toggle_overrides_button.clicked.connect(self._toggle_error_overrides)
        self.reset_overrides_button.clicked.connect(self._reset_error_overrides)
        self.dp5_refresh_button.clicked.connect(
            lambda _checked=False: self.dp5_refresh_requested.emit()
        )
        self.dp5_details_button.clicked.connect(self._toggle_dp5_details)
        self.dp5_self_check_button.clicked.connect(lambda: self.dp5_self_check_requested.emit())
        for name in ("header_margin", "footer_margin"):
            for control in (getattr(self, f"pdf_{name}_spinbox"), getattr(self, f"pdf_{name}_slider")):
                control.valueChanged.connect(
                    lambda _value, name=name: self._forget_preserved_margin_value(name)
                )

    def add_appearance_settings(self, widget: QWidget) -> None:
        """Embed desktop appearance in the same scrollable Settings destination."""
        self.content_layout.insertWidget(3, widget)

    @staticmethod
    def _collapsed_card(title: str, card: QWidget) -> CollapsibleSection:
        section = CollapsibleSection(title)
        section.content_layout.addWidget(card)
        return section

    @Slot()
    def _show_advanced_configuration(self) -> None:
        try:
            dialog = AdvancedConfigDialog(self.build_config(), ConfigManager.load_factory(), self)
        except (ValueError, ConfigError) as exc:
            QMessageBox.warning(self, "Advanced Configuration", str(exc))
            return
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.validated_config is not None:
            config = dialog.validated_config
            self.load_from_config(config, confirmed=False)
            self.config_loaded.emit(config)
            self.status_message.emit("info", "Updated Settings. Confirm Changes to apply them to the next run.")
            for notice in dialog.migration_notices:
                self.migration_notice.emit(notice)

    def load_from_config(self, config: AppConfig, *, confirmed: bool = True) -> None:
        """Populate the page from a validated AppConfig instance."""
        self._loading = True
        self.setUpdatesEnabled(False)
        self.errors_table.setUpdatesEnabled(False)
        self.solvents_table.setUpdatesEnabled(False)
        try:
            self.input_path_edit.setText(str(config.paths.input_path))
            self.output_dir_edit.setText(str(config.paths.output_dir))
            self.libreoffice_path_edit.setText(config.conversion.libreoffice_path or "")
            self.conversion_timeout_spinbox.setValue(config.conversion.timeout_seconds)

            self.default_deep_name_analysis_control.set_value(config.modes.deep_name_analysis)
            self.default_output_mode_control.set_value(config.modes.output_mode)
            self.open_reports_checkbox.setChecked(config.modes.open_reports_on_success)
            self.pdf_header_margin_spinbox.setValue(
                min(
                    int(round(config.modes.header_margin * 100)),
                    self.pdf_header_margin_spinbox.maximum(),
                )
            )
            self.pdf_footer_margin_spinbox.setValue(
                min(
                    int(round(config.modes.footer_margin * 100)),
                    self.pdf_footer_margin_spinbox.maximum(),
                )
            )
            self._preserve_unrepresentable_margins(config)
            enabled_blocker = QSignalBlocker(self.dp5_enabled_checkbox)
            self.dp5_enabled_checkbox.setChecked(config.dp5.enabled)
            del enabled_blocker
            self._show_dp5_value(self.dp5_threshold_spinbox, config.dp5.score_threshold_percent)
            self._show_dp5_value(self.dp5_timeout_spinbox, config.dp5.timeout_seconds)
            self._preserve_unrepresentable_dp5_values(config.dp5)
            self.dp5_keep_artifacts_checkbox.setChecked(config.dp5.keep_artifacts)
            self.default_severity_control.set_value(config.errors.default_severity)

            for row_index, error_code in enumerate(AnalysisErrorCode):
                self.errors_table.item(row_index, 1).setData(
                    Qt.ItemDataRole.UserRole, config.errors.overrides.get(error_code))

            self._set_error_overrides_visible(False)
            self._refresh_error_override_states()

            self.solvents_table.setRowCount(0)
            for solvent in config.solvents.entries:
                self._append_solvent_row(
                    solvent.name,
                    ", ".join(solvent.aliases),
                    self._format_float_list(solvent.proton_shifts),
                    self._format_float_list(solvent.carbon_shifts),
                )
                blocker = QSignalBlocker(self.solvents_table)
                row = self.solvents_table.rowCount() - 1
                for column, value in enumerate((solvent.name, solvent.aliases, solvent.proton_shifts, solvent.carbon_shifts)):
                    self.solvents_table.item(row, column).setData(Qt.ItemDataRole.UserRole, value)
                del blocker
            self._loaded_text_values = {
                control: (control.text(), value)
                for control, value in (
                    (self.input_path_edit, str(config.paths.input_path)),
                    (self.output_dir_edit, str(config.paths.output_dir)),
                    (self.libreoffice_path_edit, config.conversion.libreoffice_path),
                )
            }
        finally:
            self.solvents_table.setUpdatesEnabled(True)
            self.errors_table.setUpdatesEnabled(True)
            self.setUpdatesEnabled(True)
            self._loading = False
        if confirmed:
            self._confirmed_config = config.model_copy(deep=True)
            self._confirmed_state = self._settings_state()
            self._saved_state = self._confirmed_state
        self._refresh_change_state()

    def build_config(self) -> AppConfig:
        """Validate the current widget values into a fresh AppConfig."""
        self.clear_validation_feedback()
        input_path = self._default_input_value()
        if input_path is None:
            self._validation_widget = self.input_path_edit
            raise ValueError("Default input path cannot be empty before settings are loaded.")
        try:
            output_dir = self._path_control_value(self.output_dir_edit, "Output parent directory")
        except ValueError:
            self._validation_widget = self.output_dir_edit
            raise

        default_severity = self._segmented_value(self.default_severity_control, ErrorSeverity)
        overrides: dict[AnalysisErrorCode, ErrorSeverity] = {}
        for row_index, error_code in enumerate(AnalysisErrorCode):
            severity = self._error_override_value(row_index)
            if severity is not None:
                overrides[error_code] = severity

        solvent_entries = []
        for row_index in range(self.solvents_table.rowCount()):
            name = self._solvent_cell_value(row_index, 0)
            if not name and not self._has_preserved_solvent_value(row_index, 0):
                self._validation_widget = self.solvents_table
                self._validation_cell = (row_index, 0)
                raise ValueError(f"Solvent row {row_index + 1} is missing a name.")

            aliases = self._solvent_cell_value(row_index, 1)
            proton_shifts = self._solvent_cell_value(row_index, 2)
            carbon_shifts = self._solvent_cell_value(row_index, 3)

            solvent_entries.append(
                {
                    "name": name,
                    "aliases": aliases,
                    "proton_shifts": proton_shifts,
                    "carbon_shifts": carbon_shifts,
                }
            )

        return AppConfig.model_validate(
            {
                "paths": {
                    "input_path": input_path,
                    "output_dir": output_dir,
                },
                "modes": {
                    "deep_name_analysis": self._combo_value(
                        self.default_deep_name_analysis_control,
                        DeepNameAnalysis,
                    ),
                    "output_mode": self._combo_value(
                        self.default_output_mode_control,
                        OutputMode,
                    ),
                    "open_reports_on_success": self.open_reports_checkbox.isChecked(),
                    "header_margin": self._margin_control_value("header_margin"),
                    "footer_margin": self._margin_control_value("footer_margin"),
                },
                "conversion": {
                    "libreoffice_path": self._text_control_value(self.libreoffice_path_edit),
                    "timeout_seconds": self.conversion_timeout_spinbox.value(),
                },
                "dp5": self.dp5_settings().model_dump(mode="python"),
                "solvents": {"entries": solvent_entries},
                "errors": {
                    "default_severity": default_severity,
                    "overrides": overrides,
                },
            }
        )

    @Slot()
    def _restore_default_config(self) -> None:
        try:
            config = resolve_config_paths(ConfigManager.load_factory())
        except ConfigError as exc:
            QMessageBox.warning(self, "Return to Default Settings", str(exc))
            self.status_message.emit("critical", str(exc))
            return

        if not self._request_replacement():
            return
        self.load_from_config(config, confirmed=False)
        self.config_source_label.setText("Source: packaged factory settings in the Settings draft")
        self.default_config_restored.emit(config)
        self.status_message.emit("info", "Restored factory settings in the draft. Confirm Changes to apply them.")

    @Slot()
    def _save_default_config(self, *, ask: bool = True) -> bool:
        try:
            destination = user_config_path()
            config = resolve_config_paths(self.build_config())
            manager, _ = self._save_manager(destination)
        except Exception as exc:  # noqa: BLE001 - surface form validation to the user
            QMessageBox.warning(self, "Update Default Settings", str(exc))
            self.status_message.emit("critical", str(exc))
            return False

        response = QMessageBox.question(
            self,
            "Update Default Settings",
            f"Save these values as your user default settings?\n\n{destination}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) if ask else QMessageBox.StandardButton.Yes
        if response != QMessageBox.StandardButton.Yes:
            return False

        if self._save_with_conflict_check(manager, config, "Update Default Settings"):
            self.status_message.emit("info", f"Updated default settings in {destination}")
            return True
        return False

    @Slot()
    def _load_config_from_file(self) -> None:
        start_dir = self._directory_hint(self.output_dir_edit.text())
        selected_path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Config",
            start_dir,
            "TOML Files (*.toml)",
        )
        if not selected_path:
            return

        manager = ConfigManager(selected_path)
        try:
            manager.resolve_paths(manager.load())
            if not self._request_replacement():
                return
            # Saving the current draft may have replaced this same file.
            self._apply_loaded(manager)
        except ConfigError as exc:
            QMessageBox.warning(self, "Import Config", str(exc))
            self.status_message.emit("critical", str(exc))
            return
        self.status_message.emit("info", f"Loaded configuration from {selected_path}")

    def _apply_loaded(self, manager: ConfigManager) -> None:
        """Load the editor and revision together, making this the current save source."""
        config = manager.resolve_paths(manager.load())
        self.load_from_config(config, confirmed=False)
        self.set_config_source(manager)
        self._saved_state = self._settings_state()
        self._refresh_change_state()
        self.config_loaded.emit(config)
        for notice in manager.migration_notices:
            self.migration_notice.emit(notice)

    @Slot()
    def _save_config(self) -> None:
        try:
            config = resolve_config_paths(self.build_config())
        except Exception as exc:  # noqa: BLE001 - surface form validation to the user
            QMessageBox.warning(self, "Export Config", str(exc))
            self.status_message.emit("critical", str(exc))
            return

        start_dir = self._directory_hint(self.output_dir_edit.text())
        selected_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Config",
            str(Path(start_dir) / "settings.toml"),
            "TOML Files (*.toml)",
            options=QFileDialog.Option.DontUseNativeDialog | QFileDialog.Option.DontConfirmOverwrite,
        )
        if not selected_path:
            return

        if not selected_path.lower().endswith(".toml"):
            selected_path = f"{selected_path}.toml"

        try:
            manager, confirm_overwrite = self._save_manager(Path(selected_path))
        except ConfigError as exc:
            QMessageBox.warning(self, "Export Config", str(exc))
            self.status_message.emit("critical", str(exc))
            return
        if confirm_overwrite and QMessageBox.question(
            self, "Replace Configuration",
            f"Replace the existing settings with your current edits?\n\n{manager.config_path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        if self._save_with_conflict_check(manager, config, "Export Config"):
            self.status_message.emit("info", f"Saved configuration to {selected_path}")

    def set_config_source(self, manager: ConfigManager) -> None:
        """Keep only the current loaded/saved source and its observed revision."""
        self._source_manager = manager
        source = "packaged factory defaults" if manager.uses_factory_defaults else str(manager.config_path)
        self.config_source_label.setText(f"Source: {source}")
        self._refresh_change_state()

    def _text_control_value(self, control: QLineEdit) -> str | None:
        loaded = self._loaded_text_values.get(control)
        if loaded is not None and control.text() == loaded[0]:
            return loaded[1]
        return control.text().strip() or None

    def _path_control_value(self, control: QLineEdit, label: str) -> str:
        value = self._text_control_value(control)
        if value is None:
            raise ValueError(f"{label} cannot be empty.")
        return value

    def _default_input_value(self) -> str | None:
        value = self._text_control_value(self.input_path_edit)
        if value is None and self._confirmed_config is not None:
            return str(self._confirmed_config.paths.input_path)
        return value

    def clear_validation_feedback(self) -> None:
        """Discard a previous failure target before validating another operation."""
        self._validation_widget = None
        self._validation_cell = None
        self.validation_error_label.hide()

    def set_run_active(self, active: bool) -> None:
        """Explain when edited settings take effect during a frozen run."""
        self._run_active = active
        self._refresh_change_state()

    def _settings_state(self) -> tuple:
        """Compare effective values while retaining invalid text without showing errors."""
        input_path = self._default_input_value()
        output_dir = self._text_control_value(self.output_dir_edit)
        return (
            (str(Path(input_path)) if input_path is not None else None,
             str(Path(output_dir)) if output_dir is not None else None,
             self._text_control_value(self.libreoffice_path_edit)),
            self.conversion_timeout_spinbox.value(),
            self.default_deep_name_analysis_control.current_data(),
            self.default_output_mode_control.current_data(),
            self.open_reports_checkbox.isChecked(),
            self._margin_control_value("header_margin"), self._margin_control_value("footer_margin"),
            self.dp5_settings().model_dump(), self.default_severity_control.current_data(),
            tuple(self._error_override_value(row)
                  for row in range(self.errors_table.rowCount())),
            tuple(tuple(self._solvent_state(row, column) for column in range(4))
                  for row in range(self.solvents_table.rowCount())),
        )

    def _solvent_state(self, row: int, column: int) -> str:
        try:
            return repr(self._solvent_cell_value(row, column, mark_invalid=False))
        except ValueError:
            return repr(("invalid", self._table_text(row, column)))

    def has_unconfirmed_changes(self) -> bool:
        """Return whether the editor differs from the confirmed session."""
        return self._confirmed_state is not None and self._settings_state() != self._confirmed_state

    def has_unsaved_changes(self) -> bool:
        """Return whether the editor differs from its loaded or saved file."""
        return self._saved_state is not None and self._settings_state() != self._saved_state

    def confirmed_config(self) -> AppConfig:
        """Return an isolated copy of the settings approved for the next run."""
        if self._confirmed_config is None:
            raise ValueError("Confirm Settings before running analysis.")
        return self._confirmed_config.model_copy(deep=True)

    def _refresh_change_state(self, *_args) -> None:
        if self._loading or self._confirmed_state is None:
            return
        pending = self.has_unconfirmed_changes()
        self.confirm_button.setEnabled(pending)
        prefix = "The active check keeps its starting settings. " if self._run_active else ""
        if pending:
            message = "Unconfirmed changes. Confirm Changes to use them on the next run."
        elif self.has_unsaved_changes():
            message = "Changes confirmed for this session, but not saved to a file."
        elif self._source_manager is not None and not self._source_manager.uses_factory_defaults:
            message = f"Settings confirmed for this session. Configuration file: {self._source_manager.config_path}."
        else:
            message = "Settings confirmed for this session."
        self.config_persistence_label.setText(
            prefix + message + " Save User Defaults sets defaults loaded on the next normal launch; Export Config writes a chosen file."
        )

    @Slot()
    def confirm_changes(self) -> bool:
        """Validate and apply the draft to this session without writing a file."""
        try:
            config = self.build_config()
        except ValueError as exc:
            self.focus_validation_error(exc)
            if not self.validation_error_label.isVisible():
                self.validation_error_label.setText(str(exc)[:600])
                self.validation_error_label.show()
            return False
        self._confirmed_config = config.model_copy(deep=True)
        self._confirmed_state = self._settings_state()
        self._refresh_change_state()
        self.config_confirmed.emit(config)
        return True

    def request_confirmation(self) -> bool:
        """Let the user confirm, discard, or continue editing the draft."""
        if not self.has_unconfirmed_changes():
            return True
        choice = self._choose_settings_action(
            "Unconfirmed Settings", "Confirm your Settings changes before continuing?",
            "Confirm Changes uses this draft for the session. Discard Changes restores the last confirmed settings. "
            "Keep Editing returns to your draft.",
            accept="Confirm Changes", discard="Discard Changes", default="Confirm Changes",
        )
        if choice == "Discard Changes":
            self.load_from_config(self.confirmed_config(), confirmed=False)
            self._confirmed_state = self._settings_state()
            self.clear_validation_feedback()
            if self._source_manager is not None:
                self.set_config_source(self._source_manager)
            else:
                self.config_source_label.setText("Source: startup session configuration")
            self._refresh_change_state()
            return True
        return choice == "Confirm Changes" and self.confirm_changes()

    def request_close(self) -> bool:
        """Offer to save settings before the session and its draft are lost."""
        if not self.has_unsaved_changes() and not self.has_unconfirmed_changes():
            return True
        choice = self._choose_settings_action(
            "Unsaved Settings", "Save your current settings before closing?",
            "Save User Defaults sets defaults for the next normal launch. Close Without Saving loses changes "
            "that have not been saved to a file.",
            accept="Save User Defaults", discard="Close Without Saving", default="Save User Defaults",
        )
        if choice == "Save User Defaults":
            return self._save_default_config(ask=False)
        return choice == "Close Without Saving"

    def _request_replacement(self) -> bool:
        if not self.has_unsaved_changes() and not self.has_unconfirmed_changes():
            return True
        choice = self._choose_settings_action(
            "Replace Settings", "Save your current changes before replacing Settings?",
            "Replace Settings discards your current session edits and draft. Keep Editing preserves them.",
            accept="Save User Defaults", discard="Replace Settings", default="Keep Editing",
        )
        if choice == "Save User Defaults":
            return self._save_default_config(ask=False)
        return choice == "Replace Settings"

    def _choose_settings_action(
        self, title: str, text: str, info: str, *, accept: str, discard: str, default: str,
    ) -> str | None:
        dialog = QMessageBox(self)
        dialog.setWindowTitle(title)
        dialog.setText(text)
        dialog.setInformativeText(info)
        buttons = {
            label: dialog.addButton(label, role)
            for label, role in (
                (accept, QMessageBox.ButtonRole.AcceptRole),
                (discard, QMessageBox.ButtonRole.DestructiveRole),
                ("Keep Editing", QMessageBox.ButtonRole.RejectRole),
            )
        }
        dialog.setDefaultButton(buttons[default])
        dialog.setEscapeButton(buttons["Keep Editing"])
        dialog.exec()
        clicked = dialog.clickedButton()
        return clicked.text() if clicked is not None else None

    def focus_validation_error(self, error: Exception) -> bool:
        """Reveal the setting or solvent cell that prevented a run."""
        widget = getattr(self, "_validation_widget", None)
        if widget is None:
            return False
        self.validation_error_label.setText(str(error)[:600])
        self.validation_error_label.setToolTip(str(error))
        self.validation_error_label.show()
        parent = widget.parentWidget()
        while parent is not None and parent is not self:
            if isinstance(parent, CollapsibleSection):
                parent.set_expanded(True)
            parent = parent.parentWidget()
        if cell := getattr(self, "_validation_cell", None):
            self.solvents_table.setCurrentCell(*cell)
        scroll = self.findChild(QScrollArea)
        if scroll is not None:
            scroll.ensureWidgetVisible(widget)
        widget.setFocus(Qt.FocusReason.OtherFocusReason)
        return True

    def _has_preserved_solvent_value(self, row: int, column: int) -> bool:
        item = self.solvents_table.item(row, column)
        return item is not None and item.data(Qt.ItemDataRole.UserRole) is not None

    def _solvent_cell_value(
        self, row: int, column: int, *, mark_invalid: bool = True,
    ) -> str | list[str] | list[float]:
        if self._has_preserved_solvent_value(row, column):
            return self.solvents_table.item(row, column).data(Qt.ItemDataRole.UserRole)
        text = self._table_text(row, column)
        if column == 0:
            return text
        if column == 1:
            return self._parse_string_list(text)
        try:
            return self._parse_float_list(
                text, row_index=row,
                column_name="proton shifts" if column == 2 else "carbon shifts",
            )
        except ValueError:
            if mark_invalid:
                self._validation_widget = self.solvents_table
                self._validation_cell = (row, column)
            raise

    def _forget_preserved_solvent_value(self, item: QTableWidgetItem) -> None:
        if item.data(Qt.ItemDataRole.UserRole) is not None:
            blocker = QSignalBlocker(self.solvents_table)
            item.setData(Qt.ItemDataRole.UserRole, None)
            del blocker

    def _save_manager(self, destination: Path) -> tuple[ConfigManager, bool]:
        """Reuse the current source baseline; observe other destinations for consent."""
        path = destination.expanduser().resolve()
        if self._source_manager is not None and self._source_manager.config_path == path:
            return self._source_manager, False
        manager = ConfigManager(path)
        return manager, manager.observe_revision()

    def _save_with_conflict_check(self, manager: ConfigManager, config: AppConfig, title: str) -> bool:
        try:
            manager.save(config)
        except ConfigConflictError as exc:
            try:
                replacement = ConfigManager(manager.config_path)
                if not replacement.observe_revision():
                    self.status_message.emit(
                        "critical",
                        f"Configuration file was deleted: {manager.config_path}. "
                        "Your edits have not been saved.",
                    )
                    response = QMessageBox.question(
                        self, "Settings Changed",
                        f"The configuration file was removed: {manager.config_path}\n\n"
                        "Recreate it using your unsaved session edits? Choose No to keep editing.",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    if response == QMessageBox.StandardButton.Yes:
                        return self._save_with_conflict_check(replacement, config, title)
                    return False
                self.status_message.emit("critical", str(exc))
                response = QMessageBox.question(
                    self, "Settings Changed",
                    f"{exc}\n\nReload the latest settings and discard your unsaved session edits? "
                    "Choose No to keep editing or save to a different file.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if response == QMessageBox.StandardButton.Yes:
                    self._apply_loaded(manager)
                    self.status_message.emit("info", f"Reloaded configuration from {manager.config_path}")
            except ConfigError as load_error:
                self.status_message.emit("critical", str(exc))
                QMessageBox.warning(self, title, str(load_error))
                self.status_message.emit("critical", str(load_error))
            return False
        except ConfigError as exc:
            QMessageBox.warning(self, title, str(exc))
            self.status_message.emit("critical", str(exc))
            return False
        self.set_config_source(manager)
        self._saved_state = self._settings_state()
        self.confirm_changes()
        return True

    def dp5_settings(self) -> DP5Settings:
        """Return the current bundled DP5q settings from the editor controls."""
        return DP5Settings(
            enabled=self.dp5_enabled_checkbox.isChecked(),
            score_threshold_percent=self._dp5_control_value("score_threshold_percent", self.dp5_threshold_spinbox),
            timeout_seconds=int(self._dp5_control_value("timeout_seconds", self.dp5_timeout_spinbox)),
            keep_artifacts=self.dp5_keep_artifacts_checkbox.isChecked(),
        )

    @staticmethod
    def _show_dp5_value(spinbox, value: float) -> None:
        """Show the closest value the control can display, without overflowing it."""
        shown = min(max(value, spinbox.minimum()), spinbox.maximum())
        spinbox.setValue(shown if isinstance(spinbox, QDoubleSpinBox) else int(shown))

    def _preserve_unrepresentable_dp5_values(self, settings: DP5Settings) -> None:
        """Keep loaded values outside a control's range or precision, and say so."""
        self._dp5_preserved = {}
        for name, spinbox in (
            ("score_threshold_percent", self.dp5_threshold_spinbox),
            ("timeout_seconds", self.dp5_timeout_spinbox),
        ):
            loaded = getattr(settings, name)
            if spinbox.value() == loaded:
                spinbox.setToolTip("")
                continue
            self._dp5_preserved[name] = loaded
            reason = (
                "has more decimals than this control shows"
                if spinbox.minimum() <= loaded <= spinbox.maximum()
                else f"is outside this control's range ({spinbox.minimum():g} to {spinbox.maximum():g})"
            )
            spinbox.setToolTip(
                f"The settings file value {loaded} {reason}. Until this control is changed, "
                f"runs and saves keep {loaded}."
            )

    def _forget_preserved_dp5_value(self, name: str) -> None:
        if self._dp5_preserved.pop(name, None) is not None:
            spinbox = self.dp5_threshold_spinbox if name == "score_threshold_percent" else self.dp5_timeout_spinbox
            spinbox.setToolTip("")

    def _dp5_control_value(self, name: str, spinbox) -> float:
        return self._dp5_preserved.get(name, spinbox.value())

    def _preserve_unrepresentable_margins(self, config: AppConfig) -> None:
        """Keep loaded fractions until the user changes their percentage control."""
        self._margin_preserved = {}
        for name in ("header_margin", "footer_margin"):
            loaded = getattr(config.modes, name)
            spinbox = getattr(self, f"pdf_{name}_spinbox")
            if spinbox.value() / 100.0 == loaded:
                spinbox.setToolTip("")
                continue
            self._margin_preserved[name] = loaded
            spinbox.setToolTip(
                f"The loaded margin {loaded} ({loaded * 100:g}%) cannot be shown exactly. "
                f"Until this control is changed, runs and saves keep {loaded}."
            )

    def _forget_preserved_margin_value(self, name: str) -> None:
        if self._margin_preserved.pop(name, None) is not None:
            getattr(self, f"pdf_{name}_spinbox").setToolTip("")

    def _margin_control_value(self, name: str) -> float:
        return self._margin_preserved.get(name, getattr(self, f"pdf_{name}_spinbox").value() / 100.0)

    def set_dp5_runtime_status(self, valid: bool, messages: list[str], *, full_check: bool = False) -> None:
        """Show availability or explicit self-check diagnostics without changing edits."""
        self._dp5_runtime_valid = valid
        self._dp5_runtime_messages = list(messages)
        self._dp5_detail_lines = list(messages)
        self.dp5_status_label.setText("Bundled DP5q ready" if valid else "Bundled DP5q unavailable")
        self.dp5_status_label.setProperty("statusLevel", "ok" if valid else "warning")
        self.dp5_status_message_label.setText(
            "Ready for advisory scoring. Run Full Self-check validates the installed models like CLI `dp5 doctor`."
            if valid else (messages[0] if messages else "Repair or reinstall the CheckSI package.")
        )
        if full_check:
            self.dp5_status_label.setText("DP5q full self-check passed" if valid else "DP5q full self-check failed")
            self.dp5_status_message_label.setText(
                "Installed models passed the full self-check. Details contain the shared CLI diagnostics."
                if valid else (messages[0] if messages else "See Details for self-check diagnostics.")
            )
        self.dp5_status_label.style().unpolish(self.dp5_status_label)
        self.dp5_status_label.style().polish(self.dp5_status_label)
        self._refresh_dp5_details_text()

    def set_dp5_status_refreshing(self, refreshing: bool, *, full_check: bool = False) -> None:
        """Reflect a background metadata check or an explicit full self-check."""
        self.dp5_refresh_button.setEnabled(not refreshing)
        # Explicit full checks can be queued while metadata is refreshing.
        self.dp5_self_check_button.setEnabled(not refreshing or not full_check)
        if refreshing:
            self.dp5_status_label.setText("Checking bundled DP5q...")
            self.dp5_status_message_label.setText(
                "Running full model self-check in an isolated process. This may take a moment."
                if full_check else "Checking bundled model availability."
            )

    def set_dp5_self_check_queued(self) -> None:
        """Keep an explicit deferred request visible while another operation runs."""
        self.dp5_status_message_label.setText("Full self-check queued; it will run after the current operation finishes.")

    def set_dp5_enabled(self, enabled: bool) -> None:
        """Sync the DP5 default checkbox without emitting change signals."""
        blocker = QSignalBlocker(self.dp5_enabled_checkbox)
        self.dp5_enabled_checkbox.setChecked(enabled)
        del blocker

    @Slot()
    def _toggle_dp5_details(self) -> None:
        self._dp5_details_visible = not self._dp5_details_visible
        self.dp5_details_panel.setVisible(self._dp5_details_visible)
        self.dp5_details_button.setText(
            "Hide Details" if self._dp5_details_visible else "Show Details"
        )

    @Slot()
    def _toggle_error_overrides(self) -> None:
        self._set_error_overrides_visible(not self.error_overrides_panel.isVisible())

    @Slot()
    def _reset_error_overrides(self) -> None:
        blocker = QSignalBlocker(self.errors_table)
        for row_index in range(self.errors_table.rowCount()):
            self.errors_table.item(row_index, 1).setData(Qt.ItemDataRole.UserRole, None)
        del blocker
        self._refresh_error_override_states()

    @Slot()
    def _browse_default_input(self) -> None:
        selected_path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Default Input File",
            self._directory_hint(self.input_path_edit.text()),
            "Documents (*.pdf *.docx);;PDF Files (*.pdf);;Word Documents (*.docx)",
        )
        if selected_path:
            self.input_path_edit.setText(selected_path)

    @Slot()
    def _browse_output_directory(self) -> None:
        selected_path = QFileDialog.getExistingDirectory(
            self,
            "Choose Output Parent Directory",
            self._directory_hint(self.output_dir_edit.text()),
        )
        if selected_path:
            self.output_dir_edit.setText(selected_path)

    @Slot()
    def _show_add_solvent_dialog(self) -> None:
        dialog = SolventEditorDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        name, aliases, proton_shifts, carbon_shifts = dialog.values()
        if not name:
            QMessageBox.warning(self, "Add Solvent", "The solvent name cannot be empty.")
            return

        self._append_solvent_row(name, aliases, proton_shifts, carbon_shifts)

    @Slot()
    def _remove_selected_solvent(self) -> None:
        selection_model = self.solvents_table.selectionModel()
        if selection_model is None:
            return
        selected_rows = sorted(
            {index.row() for index in selection_model.selectedRows()},
            reverse=True,
        )
        if not selected_rows:
            QMessageBox.information(
                self,
                "Remove Solvent",
                "Select a solvent row before removing it.",
            )
            return

        response = QMessageBox.question(
            self,
            "Remove Solvent",
            "Remove the selected solvent entry?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if response != QMessageBox.StandardButton.Yes:
            return

        for row_index in selected_rows:
            self.solvents_table.removeRow(row_index)

    def _append_solvent_row(
        self,
        name: str,
        aliases: str,
        proton_shifts: str,
        carbon_shifts: str,
    ) -> None:
        row_index = self.solvents_table.rowCount()
        self.solvents_table.insertRow(row_index)
        self.solvents_table.setItem(row_index, 0, QTableWidgetItem(name))
        self.solvents_table.setItem(row_index, 1, QTableWidgetItem(aliases))
        self.solvents_table.setItem(row_index, 2, QTableWidgetItem(proton_shifts))
        self.solvents_table.setItem(row_index, 3, QTableWidgetItem(carbon_shifts))

    def _create_card(self, title: str, description: str) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setProperty("card", "true")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(12)

        title_label = QLabel(title)
        title_label.setStyleSheet("font-size: 16px; font-weight: 600;")
        description_label = QLabel(description)
        description_label.setWordWrap(True)
        description_label.setStyleSheet("opacity: 0.8;")

        card_layout.addWidget(title_label)
        card_layout.addWidget(description_label)
        return card, card_layout

    def _add_path_row(
        self,
        parent_layout: QFormLayout,
        label: str,
        line_edit: QLineEdit,
        browse_handler,
    ) -> None:
        row_layout = QHBoxLayout()
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(12)
        label_widget = self._form_label(label, FIELD_HEIGHT, line_edit)
        browse_button = StandardButton("Browse", role="secondary")
        browse_button.setProperty("compactField", "true")
        browse_button.setFixedHeight(FIELD_HEIGHT)
        browse_button.clicked.connect(browse_handler)
        row_layout.addWidget(line_edit, 1)
        row_layout.addWidget(browse_button)
        parent_layout.addRow(label_widget, row_layout)

    @staticmethod
    def _form_label(text: str, control_height: int, control: QWidget | None = None) -> QLabel:
        label = QLabel(text)
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        label.setFixedHeight(control_height)
        if control is not None:
            label.setBuddy(control)
            if isinstance(control, SingleSelectSegmentedControl):
                control.set_field_name(text)
            else:
                control.setAccessibleName(text)
        return label

    @staticmethod
    def _build_enum_segments(enum_type) -> SingleSelectSegmentedControl:
        control = SingleSelectSegmentedControl()
        for member in enum_type:
            label = member.value.replace("_", " ").title()
            control.add_option(label, member)
        if enum_type is DeepNameAnalysis:
            control.set_value(DeepNameAnalysis.OFF)
        return control

    @staticmethod
    def _build_error_override_combo() -> StandardComboBox:
        combo = StandardComboBox()
        combo.addItem("Use Default", None)
        for member in ErrorSeverity:
            label = member.value.replace("_", " ").title()
            combo.addItem(label, member)
        combo.refresh_contents_width()
        combo.setCurrentIndex(0)
        combo.setProperty("usingDefault", "true")
        return combo

    def _initialize_error_override_rows(self) -> None:
        self.errors_table.setRowCount(len(AnalysisErrorCode))
        for row_index, error_code in enumerate(AnalysisErrorCode):
            code_item = QTableWidgetItem(error_code.value)
            messages = "\n".join(ERROR_TEMPLATES.get(error_code, {}).values())
            code_item.setToolTip(messages)
            searchable = f"{error_code.value}\n{messages}".casefold()
            if error_code.value.startswith("c13_"):
                searchable += " 13c"
            code_item.setData(Qt.ItemDataRole.UserRole, searchable)
            code_item.setFlags(code_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.errors_table.setItem(row_index, 0, code_item)

            self.errors_table.setItem(row_index, 1, QTableWidgetItem("Use Default"))

    def _error_override_value(self, row: int) -> ErrorSeverity | None:
        value = self.errors_table.item(row, 1).data(Qt.ItemDataRole.UserRole)
        return ErrorSeverity(value) if value is not None else None

    def _error_override_edited(self, item: QTableWidgetItem) -> None:
        if item.column() == 1 and not self._loading:
            self._refresh_error_override_states()

    def _edit_error_override(self, index: QModelIndex) -> None:
        if index.column() == 1:
            self.errors_table.edit(index)

    @classmethod
    def _build_percentage_control(cls) -> tuple[QWidget, QSlider, QSpinBox]:
        container, slider, spinbox = cls._build_numeric_slider_control(
            minimum=0,
            maximum=30,
            step=1,
            decimals=0,
            suffix="%",
            width_sample="30%",
        )
        if not isinstance(spinbox, QSpinBox):
            raise TypeError("PDF margin controls require an integer spinbox.")
        return container, slider, spinbox

    @staticmethod
    def _build_numeric_slider_control(
        *,
        minimum: float,
        maximum: float,
        step: float,
        decimals: int,
        suffix: str,
        width_sample: str,
    ) -> tuple[QWidget, QSlider, QSpinBox | QDoubleSpinBox]:
        container = QWidget()
        container.setProperty("numericSliderControl", "true")
        container.setAutoFillBackground(False)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setProperty("settingSlider", "true")
        slider_steps = max(int(round((maximum - minimum) / step)), 1)
        slider.setRange(0, slider_steps)
        slider.setSingleStep(1)
        slider.setPageStep(max(int(round(slider_steps / 10)), 1))
        slider.setFixedHeight(24)
        slider.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        spinbox: QSpinBox | QDoubleSpinBox
        if decimals > 0:
            spinbox = QDoubleSpinBox()
            spinbox.setDecimals(decimals)
            spinbox.setRange(float(minimum), float(maximum))
            spinbox.setSingleStep(float(step))
        else:
            spinbox = QSpinBox()
            spinbox.setRange(int(round(minimum)), int(round(maximum)))
            spinbox.setSingleStep(int(round(step)))

        spinbox.setProperty("settingSpinBox", "true")
        spinbox.setSuffix(suffix)
        spinbox.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        spinbox.setAlignment(Qt.AlignmentFlag.AlignCenter)
        spinbox.setFixedHeight(FIELD_HEIGHT)
        spinbox.setFixedWidth(
            max(64, spinbox.fontMetrics().horizontalAdvance(width_sample) + 18)
        )

        def _value_from_slider(position: int) -> float:
            value = minimum + (position * step)
            return min(max(value, minimum), maximum)

        def _slider_from_value(value: float) -> int:
            return min(max(int(round((float(value) - minimum) / step)), 0), slider_steps)

        def _sync_spinbox(position: int) -> None:
            blocker = QSignalBlocker(spinbox)
            value = _value_from_slider(position)
            spinbox.setValue(value if decimals > 0 else int(round(value)))
            del blocker

        def _sync_slider(value: float) -> None:
            blocker = QSignalBlocker(slider)
            slider.setValue(_slider_from_value(value))
            del blocker

        slider.valueChanged.connect(_sync_spinbox)
        spinbox.valueChanged.connect(_sync_slider)
        spinbox.setValue(minimum if decimals > 0 else int(round(minimum)))

        layout.addWidget(slider, 1)
        layout.addWidget(spinbox)
        return container, slider, spinbox

    @staticmethod
    def _set_combo_value(combo: StandardComboBox, expected_value) -> None:
        if expected_value is None:
            combo.setCurrentIndex(0)
            return
        for index in range(combo.count()):
            if combo.itemData(index) == expected_value:
                combo.setCurrentIndex(index)
                return

    @staticmethod
    def _combo_value(
        combo: StandardComboBox | SingleSelectSegmentedControl,
        enum_type,
        *,
        allow_none: bool = False,
    ):
        if isinstance(combo, SingleSelectSegmentedControl):
            current = combo.current_data()
            if current is None and allow_none:
                return None
            if isinstance(current, enum_type):
                return current
            return enum_type(current)
        current = combo.currentData()
        if current is None and allow_none:
            return None
        if isinstance(current, enum_type):
            return current
        return enum_type(current)

    @classmethod
    def _segmented_value(cls, control: SingleSelectSegmentedControl, enum_type):
        return cls._combo_value(control, enum_type)

    def _set_error_overrides_visible(self, visible: bool) -> None:
        self.error_overrides_panel.setVisible(visible)
        self.toggle_overrides_button.setText(
            "Hide Per-Error Overrides" if visible else "Show Per-Error Overrides"
        )

    @Slot()
    def _refresh_error_override_states(self) -> None:
        has_overrides = False
        blocker = QSignalBlocker(self.errors_table)
        for row_index in range(self.errors_table.rowCount()):
            item = self.errors_table.item(row_index, 1)
            severity = self._error_override_value(row_index)
            using_default = severity is None
            has_overrides = has_overrides or not using_default
            item.setText("Use Default" if using_default else severity.value.title())
            item.setToolTip(
                "Using the default severity above."
                if using_default
                else "This row overrides the default severity."
            )
        del blocker

        self.errors_table.setProperty("usingDefaultOverrides", "true" if not has_overrides else "false")
        self.errors_table.style().unpolish(self.errors_table)
        self.errors_table.style().polish(self.errors_table)
        self.reset_overrides_button.setEnabled(has_overrides)
        self._filter_error_rows()
        self._refresh_change_state()

    def _filter_error_rows(self, *_args) -> None:
        query = self.error_search_edit.text().strip().casefold()
        changed_only = self.changed_errors_checkbox.isChecked()
        visible = 0
        for row, code in enumerate(AnalysisErrorCode):
            item = self.errors_table.item(row, 0)
            matches = query in item.data(Qt.ItemDataRole.UserRole)
            changed = self._error_override_value(row) != self._factory_overrides.get(code)
            show = matches and (not changed_only or changed)
            self.errors_table.setRowHidden(row, not show)
            visible += show
        self.error_filter_status.setText(
            f"{visible} of {len(AnalysisErrorCode)} diagnostics" if visible else "No matching diagnostics. Clear the search or filter to see more."
        )

    def _refresh_dp5_details_text(self) -> None:
        self.dp5_details_label.setText("\n".join(self._dp5_detail_lines))

    @staticmethod
    def _require_text(value: str, label: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError(f"{label} cannot be empty.")
        return cleaned

    def _table_text(self, row_index: int, column_index: int) -> str:
        item = self.solvents_table.item(row_index, column_index)
        if item is None:
            return ""
        return item.text().strip()

    @staticmethod
    def _parse_string_list(raw_value: str) -> list[str]:
        return [part.strip() for part in raw_value.split(",") if part.strip()]

    @staticmethod
    def _parse_float_list(raw_value: str, *, row_index: int, column_name: str) -> list[float]:
        if not raw_value.strip():
            return []

        values = []
        for token in raw_value.split(","):
            cleaned = token.strip()
            if not cleaned:
                continue
            try:
                values.append(float(cleaned))
            except ValueError as exc:
                raise ValueError(
                    f"Invalid {column_name} in solvent row {row_index + 1}: '{cleaned}'"
                ) from exc
        return values

    @staticmethod
    def _format_float_list(values: list[float]) -> str:
        return ", ".join(repr(value) for value in values)

    @staticmethod
    def _directory_hint(raw_value: str, fallback: Path | None = None) -> str:
        if raw_value.strip():
            candidate = Path(raw_value).expanduser()
            return str(candidate.parent if candidate.suffix else candidate)
        return str(fallback or Path.cwd())
