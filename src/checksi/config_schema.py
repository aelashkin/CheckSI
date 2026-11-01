"""Typed configuration schema for CheckSI."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
import sys
from typing import Any
import warnings

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from checksi.errors.defs import AnalysisErrorCode

ErrorKey = AnalysisErrorCode


class DeepNameAnalysis(StrEnum):
    """Whether local OPSIN performs deep name analysis."""

    ON = "on"
    OFF = "off"


class OutputMode(StrEnum):
    """Output verbosity level."""

    ESSENTIAL = "essential"
    DETAILED = "detailed"


class ErrorSeverity(StrEnum):
    """Error severity used by error toggles."""

    DISABLED = "disabled"
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class StrictConfigModel(BaseModel):
    """Common model configuration for all config sections."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class PathSettings(StrictConfigModel):
    """Filesystem paths used by the application."""

    input_path: Path
    output_dir: Path


class ConversionSettings(StrictConfigModel):
    """Optional LibreOffice executable and bounded conversion time."""

    libreoffice_path: str | None = None
    timeout_seconds: int = Field(default=60, ge=1, le=3600)

    @field_validator("libreoffice_path", mode="before")
    @classmethod
    def normalize_blank_executable(cls, value: Any) -> Any:
        """Use auto-discovery for empty CLI, TOML and GUI values alike."""
        return None if isinstance(value, str) and not value.strip() else value


class ModeSettings(StrictConfigModel):
    """Modes that alter runtime behavior."""

    deep_name_analysis: DeepNameAnalysis = DeepNameAnalysis.OFF
    output_mode: OutputMode
    open_reports_on_success: bool = False
    header_margin: float = Field(default=0.05, ge=0.0, le=0.45)
    footer_margin: float = Field(default=0.05, ge=0.0, le=0.45)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_operating_mode(cls, data: Any, info: ValidationInfo) -> Any:
        """Read old TOML settings while exposing and saving only the new switch."""
        if not isinstance(data, dict) or "operating_mode" not in data:
            return data
        migrated = dict(data)
        old_value = migrated.pop("operating_mode")
        if old_value not in ("offline", "online"):
            raise ValueError("Legacy modes.operating_mode must be offline or online.")
        canonical = {"offline": "off", "online": "on"}[old_value]
        if "deep_name_analysis" in migrated and migrated["deep_name_analysis"] != canonical:
            raise ValueError("Conflicting modes.deep_name_analysis and modes.operating_mode settings.")
        migrated.setdefault("deep_name_analysis", canonical)
        _migration_notice(
            "modes.operating_mode is deprecated; use modes.deep_name_analysis = on/off. "
            "An explicit save writes the canonical key. Name resolution uses local OPSIN only.",
            info,
        )
        return migrated


class SolventEntry(StrictConfigModel):
    """Single solvent definition with expected residual peaks."""

    name: str
    aliases: list[str] = Field(default_factory=list)
    proton_shifts: list[float] = Field(default_factory=list)
    carbon_shifts: list[float] = Field(default_factory=list)


class SolventSettings(StrictConfigModel):
    """Solvent configuration section."""

    entries: list[SolventEntry]


class ErrorSettings(StrictConfigModel):
    """Per-error severity configuration."""

    default_severity: ErrorSeverity
    overrides: dict[ErrorKey, ErrorSeverity] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_error_alias(cls, data: Any) -> Any:
        """Load the candidate key as one effective canonical override."""
        if not isinstance(data, dict) or not isinstance(data.get("overrides"), dict):
            return data
        alias = "nmr_solvent_and_frequency_missing"
        if alias not in data["overrides"]:
            return data
        overrides = dict(data["overrides"])
        alias_severity = overrides.pop(alias)
        overrides.setdefault("nmr_solvent_or_frequency_missing", alias_severity)
        return {**data, "overrides": overrides}


def _migration_notice(message: str, info: ValidationInfo) -> None:
    """Collect file-load notices explicitly; keep direct schema calls observable."""
    notices = info.context.get("migration_notices") if isinstance(info.context, dict) else None
    if isinstance(notices, list):
        notices.append(message)
    else:
        # Pydantic adds wrapper frames; attribute direct-schema warnings to its caller.
        frame = sys._getframe(1)
        stacklevel = 2
        while frame is not None and stacklevel < 32:
            module = frame.f_globals.get("__name__", "")
            if module != __name__ and not module.startswith("pydantic."):
                break
            frame = frame.f_back
            stacklevel += 1
        warnings.warn(message, UserWarning, stacklevel=stacklevel)


class DP5Settings(StrictConfigModel):
    """Optional bundled DP5q scoring settings, with load-only legacy migration."""

    enabled: bool = False
    score_threshold_percent: float = Field(default=20.0, ge=0.0, le=100.0)
    timeout_seconds: int = Field(default=600, ge=1)
    keep_artifacts: bool = False

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_settings(cls, data: Any, info: ValidationInfo) -> Any:
        """Accept known old settings without retaining external-runtime controls."""
        if not isinstance(data, dict):
            return data
        migrated = dict(data)
        old_key = "probability_threshold_percent"
        new_key = "score_threshold_percent"
        if old_key in migrated:
            old_value = migrated.pop(old_key)
            if new_key in migrated:
                try:
                    equal_values = float(old_value) == float(migrated[new_key])
                except (TypeError, ValueError) as exc:
                    raise ValueError("DP5 thresholds must be numeric.") from exc
                if not equal_values:
                    raise ValueError(f"Conflicting DP5 threshold settings: {old_key} and {new_key}.")
            migrated.setdefault(new_key, old_value)
            _migration_notice(
                "dp5.probability_threshold_percent is deprecated; use dp5.score_threshold_percent. "
                "An explicit save writes the canonical key.",
                info,
            )
        obsolete = {"managed", "install_root", "expected_rev"}.intersection(migrated)
        if obsolete:
            _migration_notice(
                "Legacy DP5 runtime settings are no longer used: "
                + ", ".join(sorted(obsolete))
                + ". DP5q is bundled; an explicit save removes these settings.",
                info,
            )
            for key in obsolete:
                del migrated[key]
        return migrated


class AppConfig(StrictConfigModel):
    """Root application configuration."""

    paths: PathSettings
    modes: ModeSettings
    solvents: SolventSettings
    errors: ErrorSettings
    dp5: DP5Settings = Field(default_factory=DP5Settings)
    conversion: ConversionSettings = Field(default_factory=ConversionSettings)
