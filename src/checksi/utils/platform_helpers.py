import os
import subprocess
import sys

from checksi.utils.document_conversion import (
    ConversionCancelled as ConversionCancelled,
    convert_docx_to_pdf as convert_docx_to_pdf,
)


def open_system_file(filepath: str, log=None) -> bool:
    """Open a file with the platform-default desktop application."""
    _log = log or print

    try:
        if sys.platform.startswith("win"):
            os.startfile(filepath)  # type: ignore[attr-defined]
        elif sys.platform.startswith("darwin"):
            subprocess.run(["open", filepath], check=True)
        elif sys.platform.startswith("linux"):
            subprocess.run(["xdg-open", filepath], check=True)
        else:
            _log(f"Unsupported platform for opening files: {sys.platform}")
            return False
    except Exception as exc:  # noqa: BLE001
        _log(f"Failed to open the file {filepath}: {exc}")
        return False

    return True
