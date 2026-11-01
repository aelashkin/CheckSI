"""Gaussian quantile fits and numerically stable DP5q compatibility scores."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable

import numpy as np
from scipy.optimize import least_squares
from scipy.special import log_ndtr, logsumexp, ndtr


class ScoringError(ValueError):
    """Predictions cannot support a finite, identifiable compatibility score."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True)
class GaussianFit:
    """One positive-width fit, with numerical rather than probability diagnostics."""

    mu: float
    sigma: float
    residual_rms: float
    crossing_count: int
    evaluations: int
    condition_number: float | None


@dataclass(frozen=True)
class CompatibilityScore:
    """Raw score and log values before any display scaling."""

    score: float
    log_score: float
    group_log_scores: np.ndarray


def fit_gaussian(quantiles, levels, *, cancel_check: Callable[[], None] | None = None) -> GaussianFit:
    """Fit the existing CDF least-squares objective with a positive log width.

    Normalize the fit coordinates for conditioning. Quantiles remain paired
    with their original levels; crossings are diagnostic and are never sorted.
    """
    values = np.asarray(quantiles, dtype=np.float64)
    percentiles = np.asarray(levels, dtype=np.float64)
    if (values.ndim != 1 or len(values) < 3 or values.shape != percentiles.shape
            or not np.isfinite(values).all() or not np.isfinite(percentiles).all()
            or np.any((percentiles <= 0) | (percentiles >= 1))
            or np.any(np.diff(percentiles) <= 0)):
        raise ScoringError("invalid_quantiles", "Quantiles and levels must be finite paired vectors with increasing levels.")
    center = float(np.median(values))
    scale = float(np.std(values))
    if not math.isfinite(scale) or scale <= 64 * np.finfo(float).eps * max(1.0, abs(center)):
        raise ScoringError("unidentifiable_distribution", "Flat quantiles do not identify a positive distribution width.")
    normalized = (values - center) / scale

    def residual(parameters):
        if cancel_check is not None:
            cancel_check()
        # These are floating-point representability bounds, not a replacement
        # physical sigma. A saturated/nonfinite final width is rejected below.
        inverse = np.exp(np.clip(-parameters[1], -700.0, 700.0))
        with np.errstate(over="ignore", invalid="ignore"):
            z = (normalized - parameters[0]) * inverse
        return ndtr(z) - percentiles

    def jacobian(parameters):
        inverse = np.exp(np.clip(-parameters[1], -700.0, 700.0))
        with np.errstate(over="ignore", under="ignore", invalid="ignore"):
            z = (normalized - parameters[0]) * inverse
            density = np.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
            width_derivative = -z * density
        width_derivative = np.where(np.isfinite(z), width_derivative, 0.0)
        return np.column_stack((-density * inverse, width_derivative))

    result = least_squares(residual, [0.0, 0.0], jac=jacobian, method="trf",
                           max_nfev=300, ftol=1e-12, xtol=1e-12, gtol=1e-12)
    with np.errstate(over="ignore", under="ignore"):
        sigma = float(np.exp(result.x[1]) * scale)
    mu = float(center + result.x[0] * scale)
    if not result.success or not math.isfinite(mu) or not math.isfinite(sigma) or sigma <= 0:
        raise ScoringError("distribution_fit_failed", "Gaussian CDF fitting did not produce a finite positive-width result.")
    # sigma -> infinity permits a constant CDF. Its least-squares optimum is
    # exactly mean(levels). A finite distribution must improve on that boundary
    # by more than floating-point objective resolution, regardless of where the
    # optimizer stops. This is dimensionless, not a calibrated ppm/residual cap.
    boundary_sse = float(np.sum((percentiles - percentiles.mean()) ** 2))
    fitted_sse = float(result.fun @ result.fun)
    objective_resolution = 64 * np.finfo(float).eps * max(1.0, boundary_sse)
    if boundary_sse - fitted_sse <= objective_resolution:
        raise ScoringError("unidentifiable_distribution", "A finite Gaussian fit does not improve on the constant-CDF infinite-width boundary.")
    # A ratio-only condition number misses BOTH singular values vanishing as
    # sigma tends to infinity. Parameters are normalized, so absolute local
    # sensitivity below sqrt(machine epsilon) cannot identify a finite fit.
    sensitivity = np.linalg.svd(result.jac, compute_uv=False)
    if (not np.isfinite(sensitivity).all()
            or sensitivity[-1] <= math.sqrt(np.finfo(float).eps)):
        raise ScoringError("unidentifiable_distribution", "Quantiles do not identify a numerically resolvable finite Gaussian distribution.")
    condition = float(np.linalg.cond(result.jac))
    return GaussianFit(mu, sigma, float(np.sqrt(np.mean(result.fun ** 2))),
                       int(np.sum(np.diff(values) < 0)), int(result.nfev),
                       condition if math.isfinite(condition) else None)


def _evidence_weights(value, count):
    """Validate positive scored-row evidence; omitted rows are excluded upstream."""
    weights = np.ones(count) if value is None else np.asarray(value, dtype=np.float64)
    if (weights.shape != (count,) or not np.isfinite(weights).all()
            or np.any(weights <= 0) or np.any(weights > 1)):
        raise ScoringError('invalid_evidence_weights', 'Scored evidence weights must be aligned, finite and in(0,1].')
    return weights


def aggregate_compatibility(observed, mus, sigmas, populations, *, evidence_weights=None) -> CompatibilityScore:
    """Mix per-conformer two-tail values, then geometrically average groups.

    This is a compatibility score, not a posterior probability of correctness.
    No epsilon is added to any group and Gaussian parameters are never averaged.
    """
    values = np.asarray(observed, dtype=np.float64)
    locations = np.asarray(mus, dtype=np.float64)
    widths = np.asarray(sigmas, dtype=np.float64)
    weights = np.asarray(populations, dtype=np.float64)
    if (values.ndim != 1 or not len(values) or locations.ndim != 2
            or locations.shape != widths.shape or locations.shape[1] != len(values)
            or weights.shape != (locations.shape[0],)
            or not all(np.isfinite(array).all() for array in (values, locations, widths, weights))
            or np.any(widths <= 0) or np.any(weights < 0)
            or not math.isclose(float(weights.sum()), 1.0, rel_tol=1e-12, abs_tol=1e-12)):
        raise ScoringError("invalid_distribution", "Complete finite distributions and normalized populations are required.")
    with np.errstate(over="ignore", divide="ignore", under="ignore", invalid="ignore"):
        z = (values[None, :] - locations) / widths
        log_tails = math.log(2.0) + log_ndtr(-np.abs(z))
        group_logs = logsumexp(np.log(weights)[:, None] + log_tails, axis=0)
    if np.isnan(group_logs).any() or np.any(group_logs > 1e-12):
        raise ScoringError("invalid_compatibility", "Compatibility aggregation produced an invalid value.")
    group_logs = np.minimum(group_logs, 0.0)
    evidence = _evidence_weights(evidence_weights, len(values))
    log_score = min(0.0, float(np.mean(group_logs) if np.all(evidence == 1)
                             else (evidence @ group_logs) / evidence.sum()))
    return CompatibilityScore(float(math.exp(log_score)), log_score, group_logs)


def diagnostic_errors(predicted, observed, populations, *, evidence_weights=None) -> dict:
    """Compute optional regression diagnostics without altering score inputs."""
    calculated = np.asarray(predicted, dtype=np.float64)
    experimental = np.asarray(observed, dtype=np.float64)
    weights = np.asarray(populations, dtype=np.float64)
    if (calculated.ndim != 2 or calculated.shape[1:] != experimental.shape
            or weights.shape != (calculated.shape[0],)
            or not all(np.isfinite(array).all() for array in (calculated, experimental, weights))):
        raise ScoringError("invalid_diagnostic_input", "Error diagnostics require finite paired shifts.")
    weighted_mean = weights @ calculated
    evidence = _evidence_weights(evidence_weights, len(experimental))
    ordinary = bool(np.all(evidence == 1))
    measurement_count = float(evidence.sum())
    raw_mae = (np.mean(np.abs(weighted_mean - experimental)) if ordinary
               else (evidence @ np.abs(weighted_mean - experimental)) / measurement_count)
    result = {"raw_mae": float(raw_mae),
              "corrected_mae": None, "scaled_errors": None, "regression_reason": None}
    if measurement_count < 2 - 64 * np.finfo(float).eps:
        result["regression_reason"] = "insufficient_signals"
        return result
    experimental_mean = (experimental.mean() if ordinary
                         else float(evidence @ experimental) / measurement_count)
    calculated_mean = (calculated.mean(axis=1) if ordinary
                       else (calculated @ evidence) / measurement_count)
    centered = experimental - experimental_mean
    denominator = float(centered @ centered if ordinary else evidence @ (centered * centered))
    norm = float(experimental @ experimental if ordinary else evidence @ (experimental * experimental))
    if denominator <= 64 * np.finfo(float).eps * max(1.0, norm):
        result["regression_reason"] = "constant_experimental_shifts"
        return result
    slopes = (calculated - calculated_mean[:, None]) @ (centered if ordinary else evidence * centered) / denominator
    if not np.isfinite(slopes).all() or np.any(np.abs(slopes) <= 64 * np.finfo(float).eps):
        result["regression_reason"] = "unidentifiable_slope"
        return result
    intercepts = calculated_mean - slopes * experimental_mean
    scaled = (calculated - intercepts[:, None]) / slopes[:, None] - experimental
    if not np.isfinite(scaled).all():
        result["regression_reason"] = "nonfinite_correction"
        return result
    result["scaled_errors"] = (weights @ scaled).tolist()
    mae = (np.mean(np.abs(scaled), axis=1) if ordinary
           else (np.abs(scaled) @ evidence) / measurement_count)
    result["corrected_mae"] = float(weights @ mae)
    return result
