"""Theme management for the PySide6 desktop application."""

from __future__ import annotations

from enum import StrEnum
from typing import Callable
from weakref import WeakMethod

from PySide6.QtCore import QObject, QSettings, Qt, Slot
from PySide6.QtGui import QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication
from shiboken6 import Shiboken

try:
    from qt_material import build_stylesheet
except ImportError:  # pragma: no cover - optional dependency
    build_stylesheet = None


class ThemePreference(StrEnum):
    """User-selectable GUI theme preference."""

    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"


class ThemeController(QObject):
    """Apply and persist the GUI theme independently from AppConfig."""

    SETTINGS_KEY = "ui/theme_preference"

    def __init__(self, app: QApplication) -> None:
        super().__init__(app)
        self._app = app
        self._settings = QSettings()
        self._style_hints = QGuiApplication.styleHints()
        self._system_theme_callback: WeakMethod | Callable[..., None] | None = None
        self._system_theme_connected = False
        self._base_style_ready = False
        self._material_style_ready = False

    def load_preference(self) -> ThemePreference:
        """Load the persisted theme preference or fall back to system."""
        raw_value = str(
            self._settings.value(
                self.SETTINGS_KEY,
                ThemePreference.SYSTEM.value,
            )
        ).strip().lower()
        try:
            return ThemePreference(raw_value)
        except ValueError:
            return ThemePreference.SYSTEM

    def save_preference(self, preference: ThemePreference) -> None:
        """Persist the selected theme preference."""
        self._settings.setValue(self.SETTINGS_KEY, preference.value)

    def apply(self, preference: ThemePreference | None = None) -> ThemePreference:
        """Apply the requested theme and return the stored preference."""
        selected = preference or self.load_preference()
        if preference is not None:
            self.save_preference(selected)

        dark_mode = self._should_use_dark_mode(selected)
        if not self._base_style_ready:
            self._app.setStyle("Fusion")
            self._base_style_ready = True
        if build_stylesheet is not None:
            theme_name = "dark_teal.xml" if dark_mode else "light_blue.xml"
            app_font_family = self._app.font().family().strip()
            base_style = build_stylesheet(
                theme=theme_name,
                invert_secondary=False,
                extra={
                    "danger": "#B3261E",
                    "warning": "#E6B300",
                    "success": "#146C2E",
                    "density_scale": "0",
                    "font_family": app_font_family or "Sans Serif",
                },
                # The pinned builder skips repeated font registration in export mode.
                export=self._material_style_ready,
            )
            self._app.setStyleSheet((base_style or "") + "\n" + self._custom_overrides(dark_mode))
            self._material_style_ready = True
        else:
            self._app.setStyleSheet(self._custom_overrides(dark_mode))

        colors = self.colors(dark_mode)
        palette = self._app.palette()
        for role, name in (
            (QPalette.ColorRole.Window, "surface"),
            (QPalette.ColorRole.Base, "surface"),
            (QPalette.ColorRole.AlternateBase, "surface_variant"),
            (QPalette.ColorRole.Button, "surface_variant"),
            (QPalette.ColorRole.WindowText, "on_surface"),
            (QPalette.ColorRole.Text, "on_surface"),
            (QPalette.ColorRole.ButtonText, "on_surface"),
            (QPalette.ColorRole.Highlight, "primary"),
            (QPalette.ColorRole.HighlightedText, "button_text"),
        ):
            palette.setColor(role, colors[name])
        palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, colors["muted"])
        self._app.setPalette(palette)
        self._app.setProperty("themeDark", dark_mode)
        self._app.setProperty("themePreference", selected.value)
        return selected

    def follow_system_theme(self, callback) -> None:
        """Re-apply the active theme when the system color scheme changes."""
        signal = getattr(self._style_hints, "colorSchemeChanged", None)
        if signal is None:
            return
        try:
            self._system_theme_callback = WeakMethod(callback)
        except TypeError:
            self._system_theme_callback = callback
        if not self._system_theme_connected:
            # Binding the window's decorated slot while its Python constructor
            # is active can crash PySide. This controller is already constructed.
            signal.connect(self._forward_system_theme)
            self._system_theme_connected = True

    @Slot(Qt.ColorScheme)
    def _forward_system_theme(self, _color_scheme: Qt.ColorScheme) -> None:
        callback = self._system_theme_callback
        if isinstance(callback, WeakMethod):
            callback = callback()
        receiver = getattr(callback, "__self__", None)
        if isinstance(receiver, QObject) and not Shiboken.isValid(receiver):
            self._system_theme_callback = None
            return
        if callback is not None:
            callback()

    def _should_use_dark_mode(self, preference: ThemePreference) -> bool:
        if preference is ThemePreference.DARK:
            return True
        if preference is ThemePreference.LIGHT:
            return False

        style_hints = QGuiApplication.styleHints()
        color_scheme = getattr(style_hints, "colorScheme", None)
        if callable(color_scheme):
            try:
                return color_scheme() == Qt.ColorScheme.Dark
            except Exception:  # noqa: BLE001 - fall back to palette heuristic
                pass

        window_color = self._app.palette().color(QPalette.ColorRole.Window)
        return window_color.lightnessF() < 0.5

    @staticmethod
    def colors(dark_mode: bool) -> dict[str, str]:
        """Return paired semantic colors for application-owned surfaces and text."""
        return {
            "surface": "#1C1B1F" if dark_mode else "#FFFBFE",
            "on_surface": "#E6E1E5" if dark_mode else "#1C1B1F",
            "surface_variant": "#2A2830" if dark_mode else "#F3EDF7",
            "outline": "#938F99" if dark_mode else "#CAC4D0",
            "muted": "#BEB8C4" if dark_mode else "#625D69",
            "primary": "#005BC4",
            "focus": "#A9C7FF" if dark_mode else "#005BC4",
            "secondary": "#303C50" if dark_mode else "#E8EEF8",
            "secondary_text": "#E6E1E5" if dark_mode else "#233B5D",
            "danger": "#B3261E",
            "warning": "#E6B300",
            "warning_text": "#362B00",
            "success": "#146C2E",
            "button_text": "#FFFFFF",
            "badge_bg": "#2F2D35" if dark_mode else "#ECE6F0",
            "critical_text": "#FFB4AB" if dark_mode else "#A31C17",
            "warning_text_on_surface": "#F3CF71" if dark_mode else "#735500",
            "info_text": "#A9C7FF" if dark_mode else "#0050AB",
        }

    @staticmethod
    def log_color(severity: str) -> str:
        """Choose a readable diagnostic foreground for the active log surface."""
        app = QApplication.instance()
        dark_mode = bool(app and app.property("themeDark"))
        name = {"critical": "critical_text", "warning": "warning_text_on_surface"}.get(severity, "info_text")
        return ThemeController.colors(dark_mode)[name]

    @classmethod
    def _custom_overrides(cls, dark_mode: bool) -> str:
        colors = cls.colors(dark_mode)
        surface = colors["surface"]
        on_surface = colors["on_surface"]
        surface_variant = colors["surface_variant"]
        outline = colors["outline"]
        muted = colors["muted"]
        primary = colors["primary"]
        focus = colors["focus"]
        secondary = colors["secondary"]
        secondary_text = colors["secondary_text"]
        danger = colors["danger"]
        warning = colors["warning"]
        success = colors["success"]
        button_text = colors["button_text"]
        badge_bg = colors["badge_bg"]

        return f"""
QMainWindow, QWidget#appRoot, QWidget#pageSurface, QScrollArea, QScrollArea > QWidget > QWidget, QStackedWidget {{
    background-color: {surface};
}}
QWidget {{
    color: {on_surface};
}}
QLabel, QCheckBox {{
    background-color: transparent;
}}
QWidget[cardSection="true"], QFrame[segmentedControl="true"] {{
    background-color: {surface_variant};
    border: none;
}}
QFrame[card="true"] {{
    background-color: {surface_variant};
    border: 1px solid {outline};
    border-radius: 14px;
}}
QLineEdit, QComboBox, QTextEdit, QTableWidget {{
    border: 1px solid {outline};
    border-radius: 10px;
    background-color: {surface};
    color: {on_surface};
    selection-background-color: {primary};
    selection-color: {button_text};
}}
QLineEdit {{
    padding: 8px 12px;
}}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QComboBox:on {{
    border: 2px solid {focus};
}}
QTextEdit[inactive="true"] {{
    color: {muted};
    background-color: {surface_variant};
}}
QLineEdit:focus, QTextEdit:focus {{
    padding: 7px 11px;
}}
QComboBox {{
    /* Let Qt choose screen-bounded list placement before showing the popup. */
    combobox-popup: 0;
    padding: 0 38px 0 12px;
}}
QComboBox:focus, QComboBox:on {{
    padding: 0 37px 0 11px;
}}
QComboBox[usingDefault="true"] {{
    color: {muted};
}}
QWidget[transparentPanel="true"],
QWidget[numericSliderControl="true"],
QWidget[numericSliderControl="true"] QWidget {{
    background-color: transparent;
}}
QWidget[detailsPanel="true"] {{
    background-color: {surface};
    border: 1px solid {outline};
    border-radius: 10px;
}}
QLabel[detailsText="true"] {{
    padding: 8px 10px;
}}
QSpinBox[settingSpinBox="true"],
QDoubleSpinBox[settingSpinBox="true"] {{
    border: 1px solid {outline};
    border-radius: 10px;
    background: {surface};
    color: {on_surface};
    padding: 0 6px;
}}
QSpinBox[settingSpinBox="true"]:focus,
QDoubleSpinBox[settingSpinBox="true"]:focus {{
    border: 2px solid {focus};
    padding: 0 5px;
}}
QSpinBox[settingSpinBox="true"]::up-button,
QSpinBox[settingSpinBox="true"]::down-button,
QDoubleSpinBox[settingSpinBox="true"]::up-button,
QDoubleSpinBox[settingSpinBox="true"]::down-button {{
    width: 0;
    height: 0;
    border: none;
    background: transparent;
}}
QSlider[settingSlider="true"] {{
    background: transparent;
    min-height: 24px;
    padding: 0;
    border: none;
}}
QSlider[settingSlider="true"]::groove:horizontal {{
    height: 8px;
    margin: 0;
    border: none;
    border-radius: 4px;
    background: {surface};
}}
QSlider[settingSlider="true"]::sub-page:horizontal {{
    margin: 0;
    border: none;
    border-radius: 4px;
    background: qlineargradient(
        x1: 0,
        y1: 0,
        x2: 1,
        y2: 0,
        stop: 0 #004CB0,
        stop: 1 {primary}
    );
}}
QSlider[settingSlider="true"]::add-page:horizontal {{
    margin: 0;
    border: none;
    border-radius: 4px;
    background: {surface};
}}
QSlider[settingSlider="true"]::handle:horizontal {{
    width: 22px;
    margin: -7px 0;
    border-radius: 11px;
    border: 1px solid #004CB0;
    background: qlineargradient(
        x1: 0,
        y1: 0,
        x2: 1,
        y2: 1,
        stop: 0 #1F74E8,
        stop: 1 {primary}
    );
}}
QSlider[settingSlider="true"]::handle:horizontal:hover,
QSlider[settingSlider="true"]::handle:horizontal:pressed {{
    border: 1px solid #00439D;
    background: qlineargradient(
        x1: 0,
        y1: 0,
        x2: 1,
        y2: 1,
        stop: 0 #2A7DF0,
        stop: 1 #0050B8
    );
}}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 32px;
    border: none;
    background: transparent;
}}
QComboBox QAbstractItemView,
QListView {{
    background-color: {surface};
    border: 1px solid {outline};
    border-radius: 10px;
    padding: 6px 0;
    selection-background-color: {primary};
    selection-color: {button_text};
    outline: 0;
}}
QListView::item {{
    min-height: 34px;
    padding: 0 12px;
}}
QCheckBox {{
    spacing: 10px;
    padding: 2px 0;
    border: none;
    outline: none;
}}
QCheckBox:focus {{
    color: {on_surface};
    border: none;
    outline: none;
}}
QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border-radius: 5px;
    border: 2px solid {outline};
    background-color: {surface};
}}
QCheckBox::indicator:checked {{
    border: 2px solid {primary};
    background-color: {primary};
}}
QCheckBox::indicator:focus {{
    border: 2px solid {focus};
}}
QCheckBox::indicator:checked:focus {{
    border: 2px solid {on_surface};
}}
QHeaderView::section {{
    background-color: {surface_variant};
    color: {on_surface};
    border: none;
    border-bottom: 1px solid {outline};
    padding: 8px;
    font-weight: 600;
}}
QPushButton, QToolButton {{
    min-height: 36px;
    padding: 0 14px;
    border-radius: 10px;
}}
QPushButton[segmentedButton="true"] {{
    background-color: transparent;
    border: 1px solid {outline};
    border-radius: 0;
    color: {on_surface};
    min-height: 36px;
    padding: 0 14px;
}}
QPushButton[segmentedButton="true"][segmentPosition="first"] {{
    border-top-left-radius: 10px;
    border-bottom-left-radius: 10px;
}}
QPushButton[segmentedButton="true"][segmentPosition="last"] {{
    border-top-right-radius: 10px;
    border-bottom-right-radius: 10px;
}}
QPushButton[segmentedButton="true"][segmentPosition="only"] {{
    border-radius: 10px;
}}
QPushButton[segmentedButton="true"]:hover {{
    background-color: {secondary};
    color: {secondary_text};
}}
QPushButton[segmentedButton="true"]:checked {{
    background-color: {primary};
    border-color: {primary};
    color: {button_text};
    font-weight: 600;
}}
QPushButton[segmentedButton="true"]:checked:hover {{
    background-color: {primary};
    color: {button_text};
}}
QPushButton[compactField="true"] {{
    min-height: 36px;
    max-height: 36px;
}}
QPushButton[role="default"] {{
    background-color: {surface_variant};
    border: 1px solid {outline};
}}
QPushButton[role="primary"] {{
    background-color: {primary};
    border: none;
    color: {button_text};
}}
QPushButton[role="secondary"] {{
    background-color: {secondary};
    border: none;
    color: {secondary_text};
}}
QPushButton[role="danger"] {{
    background-color: {danger};
    border: none;
    color: {button_text};
}}
QPushButton:disabled, QToolButton:disabled {{
    background-color: {surface_variant};
    border: 1px solid {outline};
    color: {outline};
}}
QPushButton[segmentedButton="true"]:disabled {{
    background-color: transparent;
    border: 1px solid {outline};
    color: {outline};
}}
QPushButton[segmentedButton="true"]:checked:disabled {{
    background-color: {secondary};
    border: 1px solid {secondary};
    color: {secondary_text};
}}
QToolButton[navRail="true"] {{
    background-color: {secondary};
    color: {secondary_text};
    border: none;
    border-radius: 10px;
    padding: 4px 14px;
    text-align: left;
}}
QToolButton[navRail="true"]:checked {{
    background-color: {primary};
    color: {button_text};
    font-weight: 600;
}}
QToolButton[disclosure="true"] {{
    background-color: transparent;
    color: {on_surface};
    border: 1px solid {outline};
    text-align: left;
    padding: 4px 10px;
}}
QPushButton:focus, QToolButton:focus,
QPushButton[segmentedButton="true"]:focus,
QToolButton[navRail="true"]:focus, QToolButton[disclosure="true"]:focus {{
    border: 2px solid {focus};
}}
QSlider:focus {{
    border: 2px solid {focus};
    border-radius: 8px;
}}
QLabel[badge="true"] {{
    background-color: {badge_bg};
    border-radius: 12px;
    padding: 6px 12px;
    font-weight: 600;
}}
QLabel[badgeLevel="critical"] {{
    background-color: {danger};
    color: {button_text};
}}
QLabel[badgeLevel="warning"] {{
    background-color: {warning};
    color: #362B00;
}}
QLabel[badgeLevel="success"] {{
    background-color: {success};
    color: {button_text};
}}
QLabel:disabled, QCheckBox:disabled {{
    color: {outline};
}}
QLabel[badge="true"]:disabled {{
    background-color: {surface_variant};
    color: {outline};
}}
QLineEdit:disabled, QComboBox:disabled, QTextEdit:disabled, QTableWidget:disabled {{
    color: {outline};
    background-color: {surface_variant};
}}
QTableWidget[usingDefaultOverrides="true"] {{
    color: {muted};
}}
QTableWidget[usingDefaultOverrides="true"]::item {{
    color: {muted};
}}
QProgressBar {{
    border: 1px solid {outline};
    border-radius: 8px;
    text-align: center;
    background-color: {surface_variant};
    color: {on_surface};
}}
QStatusBar {{
    background-color: {surface};
}}
QStatusBar::item {{
    border: none;
}}
QProgressBar::chunk {{
    border-radius: 7px;
    background-color: {focus};
}}
"""
