"""Error emission logic: config lookup, counting, and formatting."""

from __future__ import annotations

import string
from typing import Any, Mapping

from checksi.config_schema import AppConfig, ErrorSeverity
from checksi.errors.defs import AnalysisErrorCode
from checksi.errors.registry import ERROR_TEMPLATES
from checksi.errors.findings import ContextSnapshot, ContextSnapshotError, Finding, FindingSink
from checksi.errors.tracker import ErrorTracker


class _MissingValue:
    def __init__(self, key: str):
        self.key = key

    def __format__(self, format_spec: str) -> str:
        if format_spec:
            return "{" + self.key + ":" + format_spec + "}"
        return "{" + self.key + "}"


class _SafeFormatter(string.Formatter):
    def get_value(self, key: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        if isinstance(key, str):
            return kwargs.get(key, _MissingValue(key))
        return super().get_value(key, args, kwargs)


def _normalize_code(code: AnalysisErrorCode) -> AnalysisErrorCode:
    if not isinstance(code, AnalysisErrorCode):
        raise TypeError(f"Expected AnalysisErrorCode, got {type(code).__name__}")
    return code

_ERROR_SYMBOLS = {
    ErrorSeverity.INFO: "",
    ErrorSeverity.WARNING: "❗ ",
    ErrorSeverity.CRITICAL: "⚠️ ",
}




def error_key_for(code: AnalysisErrorCode) -> str:
    normalized = _normalize_code(code)
    return normalized.value


def get_error_severity(config: AppConfig, code: AnalysisErrorCode) -> ErrorSeverity:
    normalized = _normalize_code(code)
    return config.errors.overrides.get(normalized, config.errors.default_severity)


def get_error_level(config: AppConfig, code: AnalysisErrorCode) -> ErrorSeverity:
    return get_error_severity(config, code)


def is_error_enabled(config: AppConfig, code: AnalysisErrorCode) -> bool:
    return get_error_severity(config, code) is not ErrorSeverity.DISABLED


def _get_template(code: AnalysisErrorCode, variant: str) -> str:
    code_templates = ERROR_TEMPLATES.get(code, {})
    if variant in code_templates:
        return code_templates[variant]
    if "default" in code_templates:
        return code_templates["default"]
    return "{details}"


def _format_template(template: str, context: Mapping[str, Any]) -> str:
    formatter = _SafeFormatter()
    try:
        return formatter.format(template, **dict(context))
    except Exception:
        # Formatting failures should never crash analysis; fall back to the raw template.
        return template


def emit_error(
    config: AppConfig,
    tracker: ErrorTracker,
    code: AnalysisErrorCode,
    context: Mapping[str, Any] | None = None,
    *,
    variant: str = "default",
) -> str | None:
    """Build a formatted warning/error line based on config and registry templates."""
    normalized = _normalize_code(code)
    severity = get_error_severity(config, normalized)
    if severity is ErrorSeverity.DISABLED:
        return None
    try:
        finding = emit_finding(config, tracker, normalized, context, variant=variant)
    except ContextSnapshotError:
        # The legacy Any adapter accepted opaque objects. Keep its count and
        # formatting behavior without storing those objects in a Finding.
        tracker.add(normalized.value)
        payload = dict(context or {})
        payload.setdefault('details', 'No additional details.')
        text = _format_template(_get_template(normalized, variant), payload)
        return f'{_ERROR_SYMBOLS.get(severity, "⚠️")}{normalized.value}. {text}'
    return format_finding(finding) if finding is not None else None


def emit_finding(
    config: AppConfig,
    tracker: ErrorTracker | FindingSink,
    code: AnalysisErrorCode,
    context: Mapping[str, Any] | None = None,
    *,
    variant: str = 'default',
) -> Finding | None:
    """Capture once, returning the visible handle for ordered composition."""
    normalized = _normalize_code(code)
    severity = get_error_severity(config, normalized)
    payload = dict(context or {})
    if "details" not in payload:
        payload["details"] = "No additional details."
    finding = Finding(normalized, severity, variant, ContextSnapshot(payload),
                      tracker.source if isinstance(tracker, FindingSink) else None)
    tracker.record(finding)
    return finding if severity is not ErrorSeverity.DISABLED else None


def format_finding(finding: Finding) -> str:
    """Format solely from a captured occurrence, retaining legacy text bytes."""
    message = _format_template(_get_template(finding.code, finding.variant), finding.context)
    symbol = _ERROR_SYMBOLS.get(finding.severity, "⚠️")
    return f"{symbol}{finding.code.value}. {message}"
