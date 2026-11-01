"""Public API for centralized analysis errors."""

from __future__ import annotations

from typing import Any

from checksi.errors.defs import AnalysisErrorCode

ErrorCode = AnalysisErrorCode


def emit_error(*args: Any, **kwargs: Any):
    from checksi.errors.manager import emit_error as _emit_error

    return _emit_error(*args, **kwargs)


def error_key_for(*args: Any, **kwargs: Any):
    from checksi.errors.manager import error_key_for as _error_key_for

    return _error_key_for(*args, **kwargs)


def get_error_level(*args: Any, **kwargs: Any):
    from checksi.errors.manager import get_error_level as _get_error_level

    return _get_error_level(*args, **kwargs)


def is_error_enabled(*args: Any, **kwargs: Any):
    from checksi.errors.manager import is_error_enabled as _is_error_enabled

    return _is_error_enabled(*args, **kwargs)


__all__ = [
    "AnalysisErrorCode",
    "ErrorCode",
    "emit_error",
    "error_key_for",
    "get_error_level",
    "is_error_enabled",
]
