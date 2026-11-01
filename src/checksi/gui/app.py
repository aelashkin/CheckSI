"""Application bootstrap for the CheckSI desktop GUI."""

from __future__ import annotations

import argparse
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from checksi.config import ConfigError, ConfigManager
from checksi.main import ConfigPathError, _build_run_config
from checksi.gui_main import parse_gui_arguments
from checksi.gui.main_window import CheckSIMainWindow
from checksi.gui.theme import ThemeController


def main(argv: list[str] | argparse.Namespace | None = None) -> int:
    """Create the QApplication, load config, and show the main window."""
    args = argv if isinstance(argv, argparse.Namespace) else parse_gui_arguments(
        None if argv is None else list(argv)[1:]
    )
    app = QApplication([sys.argv[0]])
    app.setOrganizationName("CheckSI")
    app.setOrganizationDomain("local.checksi")
    app.setApplicationName("CheckSI GUI")

    theme_controller = ThemeController(app)
    theme_controller.apply()

    try:
        config_manager = ConfigManager(args.config_file) if args.config_file else ConfigManager()
        base_config = config_manager.load()
        base_config = config_manager.resolve_paths(base_config)
        args.input_file = args.input_file or str(base_config.paths.input_path)
        base_config = _build_run_config(base_config, args)
    except (ConfigError, ConfigPathError, ValueError) as exc:
        QMessageBox.critical(None, "Configuration Error", str(exc))
        return 1

    window = CheckSIMainWindow(
        base_config=base_config,
        theme_controller=theme_controller,
    )
    window.config_editor_page.set_config_source(config_manager)
    window.show_configuration_notices(config_manager.migration_notices)
    window.show()
    return app.exec()
