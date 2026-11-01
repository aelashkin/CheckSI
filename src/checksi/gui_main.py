"""Standalone entry point for the PySide6 GUI."""

from __future__ import annotations

import argparse

from checksi.main import _add_run_arguments


def parse_gui_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the desktop session layer before importing optional Qt modules."""
    parser = argparse.ArgumentParser(description="Launch the CheckSI desktop GUI.")
    parser.add_argument("--config-file", help="Explicit settings TOML file.")
    _add_run_arguments(parser, optional_input=True)
    return parser.parse_args(argv)


def main_gui(argv: list[str] | None = None) -> int:
    """Launch the GUI or print a dependency hint when Qt is unavailable."""
    args = parse_gui_arguments(argv)
    try:
        from checksi.gui.app import main as run_gui
    except ModuleNotFoundError as exc:
        if exc.name != "PySide6":
            raise
        print(
            "PySide6 is not installed. Reinstall the same trusted distribution wheel with its GUI extra "
            "and matching constraints:\n"
            "uv tool install --force --python 3.13 --constraints /absolute/path/constraints.txt "
            "'/absolute/path/checksi-0.4.0-py3-none-any.whl[gui]'\n"
            "From a source checkout, run `uv sync --locked --python 3.13 --extra gui`.\n"
            "Then run `checksi-gui`."
        )
        return 1

    return run_gui(args)


if __name__ == "__main__":
    raise SystemExit(main_gui())
