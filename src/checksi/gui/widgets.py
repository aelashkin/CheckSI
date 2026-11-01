"""Reusable Qt widget primitives for the desktop UI."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QPushButton,
    QSizePolicy,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


FIELD_HEIGHT = 36
SEGMENTED_CONTROL_HEIGHT = FIELD_HEIGHT + 2


def _interface_pixmap(name: str, extent: int, palette: QPalette,
                      mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
    """Rasterize the shared line shapes directly at their target pixel size."""
    if mode == QIcon.Mode.Disabled:
        color = palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText)
    elif name != "disclosure" and (state == QIcon.State.On or mode == QIcon.Mode.Selected):
        color = palette.color(QPalette.ColorRole.HighlightedText)
    else:
        color = palette.color(QPalette.ColorRole.WindowText)
    pixmap = QPixmap(extent, extent)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(extent / 24, extent / 24)
    painter.setPen(QPen(color, 1.8, Qt.PenStyle.SolidLine,
                       Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if name == "settings":
        for x, y in ((5, 8), (12, 16), (19, 8)):
            painter.drawLine(QPointF(x, 3), QPointF(x, y - 2))
            painter.drawLine(QPointF(x, y + 2), QPointF(x, 21))
            painter.drawEllipse(QPointF(x, y), 2, 2)
    else:
        path = QPainterPath()
        if name == "analyze":
            painter.drawRoundedRect(QRectF(4, 2.5, 16, 19), 2, 2)
            painter.drawLine(QPointF(8, 7), QPointF(16, 7))
            path.moveTo(7.5, 14)
            path.lineTo(10.5, 17)
            path.lineTo(16.5, 11)
        elif state == QIcon.State.On:
            path.moveTo(6, 9)
            path.lineTo(12, 15)
            path.lineTo(18, 9)
        else:
            path.moveTo(9, 6)
            path.lineTo(15, 12)
            path.lineTo(9, 18)
        painter.drawPath(path)
    painter.end()
    return pixmap


def interface_icon(name: str, palette: QPalette | None = None) -> QIcon:
    """Return a Qt-owned icon snapshot; UI owners refresh it on palette changes."""
    if name not in {"analyze", "settings", "disclosure"}:
        raise ValueError(f"Unknown interface icon: {name}")
    selected_palette = QApplication.palette() if palette is None else palette
    icon = QIcon()
    # Cover 16–32 logical-pixel controls at 1x–3x, plus 64-pixel previews.
    # Qt owns every pixmap and all copy/detach behavior; no Python engine survives.
    for extent in (16, 20, 24, 32, 40, 48, 60, 64, 72, 96):
        for mode in (QIcon.Mode.Normal, QIcon.Mode.Active, QIcon.Mode.Selected, QIcon.Mode.Disabled):
            for state in (QIcon.State.Off, QIcon.State.On):
                icon.addPixmap(_interface_pixmap(name, extent, selected_palette, mode, state), mode, state)
    return icon


class InterfaceToolButton(QToolButton):
    """Keep native line icons in sync with the application's semantic palette."""

    def set_interface_icon(self, name: str) -> None:
        self._interface_icon_name = name
        self._refresh_interface_icon()

    def _refresh_interface_icon(self) -> None:
        name = getattr(self, "_interface_icon_name", None)
        if name is None:
            return
        palette = QApplication.palette()
        key = (name, palette.color(QPalette.ColorRole.WindowText).rgba(),
               palette.color(QPalette.ColorRole.HighlightedText).rgba(),
               palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText).rgba())
        if key == getattr(self, "_interface_icon_key", None):
            return
        icon = interface_icon(name, palette)
        self._interface_icon_key = key
        self.setIcon(icon)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            self._refresh_interface_icon()


class CollapsibleSection(QWidget):
    """Keyboard-accessible disclosure for optional settings and run details."""

    expanded_changed = Signal(bool)

    def __init__(self, title: str, parent=None, *, expanded: bool = False) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.toggle_button = InterfaceToolButton()
        self.toggle_button.setText(title)
        self.toggle_button.setAccessibleName(title)
        self.toggle_button.setCheckable(True)
        self.toggle_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.toggle_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle_button.set_interface_icon("disclosure")
        self.toggle_button.setProperty("disclosure", "true")
        self.toggle_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.toggle_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.content = QWidget()
        self.content.setProperty("transparentPanel", "true")
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(12)
        layout.addWidget(self.toggle_button)
        layout.addWidget(self.content)
        self.toggle_button.setChecked(expanded)
        self.toggle_button.toggled.connect(self._apply_expanded)
        self._apply_expanded(expanded)

    def set_expanded(self, expanded: bool) -> None:
        """Show or hide content through the same control used by the keyboard."""
        self.toggle_button.setChecked(expanded)

    def is_expanded(self) -> bool:
        """Return whether this section's contents are disclosed."""
        return self.toggle_button.isChecked()

    def _apply_expanded(self, expanded: bool) -> None:
        self.content.setVisible(expanded)
        self.toggle_button.setAccessibleDescription("Expanded" if expanded else "Collapsed")
        self.expanded_changed.emit(expanded)


class StandardButton(QPushButton):
    """Shared button baseline for consistent sizing and spacing."""

    def __init__(self, text: str, *, role: str = "default", parent=None) -> None:
        super().__init__(text, parent)
        self.setProperty("role", role)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(36)
        self.setMinimumWidth(120)
        self.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)


class ComboBoxItemDelegate(QStyledItemDelegate):
    """Give combo-box popup items predictable height and left padding."""

    _horizontal_padding = 12
    _minimum_height = 36

    def initStyleOption(self, option: QStyleOptionViewItem, index) -> None:  # noqa: D401
        super().initStyleOption(option, index)
        option.displayAlignment = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

    def paint(self, painter, option, index) -> None:
        adjusted = QStyleOptionViewItem(option)
        self.initStyleOption(adjusted, index)
        adjusted.rect = adjusted.rect.adjusted(
            self._horizontal_padding,
            0,
            -self._horizontal_padding,
            0,
        )
        super().paint(painter, adjusted, index)

    def sizeHint(self, option, index) -> QSize:
        size = super().sizeHint(option, index)
        return QSize(
            size.width() + (self._horizontal_padding * 2),
            max(size.height(), self._minimum_height),
        )


class StandardComboBox(QComboBox):
    """Shared combo-box with native, screen-aware popup placement and readable items."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedHeight(FIELD_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMaxVisibleItems(12)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)

        popup_view = QListView(self)
        popup_view.setUniformItemSizes(True)
        popup_view.setTextElideMode(Qt.TextElideMode.ElideNone)
        popup_view.setSpacing(2)
        popup_view.setSelectionRectVisible(False)
        self.setView(popup_view)
        self.setItemDelegate(ComboBoxItemDelegate(self))

    def refresh_contents_width(self) -> None:
        """Size the closed combo-box wide enough for its longest entry."""
        if self.count() <= 0:
            return
        longest = max(len(self.itemText(index)) for index in range(self.count()))
        self.setMinimumContentsLength(max(longest, 12))

    def wheelEvent(self, event) -> None:  # noqa: D401
        """Only consume wheel changes when the combo is actively engaged."""
        if self.view().isVisible() or self.hasFocus():
            super().wheelEvent(event)
            return
        event.ignore()


class StandardLineEdit(QLineEdit):
    """Shared line-edit baseline so text fields match combo-box sizing."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(FIELD_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)


class SingleSelectSegmentedButton(QPushButton):
    """Single text-only segment used inside a segmented selector."""

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(text, parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setProperty("segmentedButton", "true")
        self.setMinimumHeight(FIELD_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)
        self._refresh_minimum_width()

    def _refresh_minimum_width(self) -> None:
        text_width = self.fontMetrics().horizontalAdvance(self.text())
        self.setMinimumWidth(max(88, text_width + 32))


class SingleSelectSegmentedControl(QFrame):
    """Exclusive segmented button row with exactly one active option."""

    selection_changed = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setProperty("segmentedControl", "true")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(SEGMENTED_CONTROL_HEIGHT)

        self._buttons: list[tuple[SingleSelectSegmentedButton, object]] = []
        self._field_name = ""
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._group.idToggled.connect(self._handle_button_toggled)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

    def add_option(self, label: str, value: object) -> None:
        """Append one selectable segment to the control."""
        button = SingleSelectSegmentedButton(label, self)
        button.setAccessibleName(f"{self._field_name}: {label}" if self._field_name else label)
        button.setProperty("segmentPosition", "only")
        self.layout().addWidget(button)
        index = len(self._buttons)
        self._buttons.append((button, value))
        self._group.addButton(button, index)
        self._refresh_segment_positions()
        self._refresh_minimum_width()
        if index == 0:
            button.setChecked(True)
            self.setFocusProxy(button)

    def set_field_name(self, name: str) -> None:
        """Associate both the composite and every focusable option with its field."""
        self._field_name = name
        self.setAccessibleName(name)
        for button, _value in self._buttons:
            button.setAccessibleName(f"{name}: {button.text()}")

    def set_value(self, expected_value: object) -> None:
        """Check the segment whose data matches ``expected_value``."""
        for button, value in self._buttons:
            if value == expected_value:
                block_group = self._group.blockSignals(True)
                block_button = button.blockSignals(True)
                button.setChecked(True)
                self.setFocusProxy(button)
                button.blockSignals(block_button)
                self._group.blockSignals(block_group)
                return

    def current_data(self) -> object | None:
        """Return the data payload for the selected segment."""
        checked = self._group.checkedButton()
        if checked is None:
            return None
        for button, value in self._buttons:
            if button is checked:
                return value
        return None

    def _refresh_segment_positions(self) -> None:
        count = len(self._buttons)
        for index, (button, _value) in enumerate(self._buttons):
            if count == 1:
                position = "only"
            elif index == 0:
                position = "first"
            elif index == count - 1:
                position = "last"
            else:
                position = "middle"
            button.setProperty("segmentPosition", position)
            button.style().unpolish(button)
            button.style().polish(button)

    def _refresh_minimum_width(self) -> None:
        layout = self.layout()
        spacing = max(len(self._buttons) - 1, 0) * (layout.spacing() if layout else 0)
        total_width = sum(button.minimumWidth() for button, _value in self._buttons) + spacing
        self.setMinimumWidth(total_width)

    def _handle_button_toggled(self, index: int, checked: bool) -> None:
        if checked and 0 <= index < len(self._buttons):
            self.setFocusProxy(self._buttons[index][0])
            self.selection_changed.emit(self._buttons[index][1])


class NavigationRailButton(InterfaceToolButton):
    """Compact left-rail navigation button."""

    def __init__(self, text: str, *, icon: QIcon | None = None,
                 icon_name: str | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setText(text)
        if icon_name is not None:
            self.set_interface_icon(icon_name)
        elif icon is not None:
            self.setIcon(icon)
        self.setCheckable(True)
        self.setAutoRaise(True)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumHeight(44)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setProperty("navRail", "true")


class SummaryBadge(QLabel):
    """Badge-style summary label used for run results."""

    def __init__(self, label: str, value: str = "", *, level: str = "default", parent=None) -> None:
        super().__init__(parent)
        self.setProperty("badge", "true")
        self.setProperty("badgeLevel", level)
        self.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.set_value(label, value)

    def set_value(self, label: str, value: str, *, level: str | None = None) -> None:
        """Update the rendered badge text and optional severity level."""
        self.setText(f"{label}: {value}" if value else label)
        if level is not None:
            self.setProperty("badgeLevel", level)
            self.style().unpolish(self)
            self.style().polish(self)
