"""Carbon-group accounting and bounded squared-error matching."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

import numpy as np
from scipy.optimize import linear_sum_assignment

from .contracts import MAX_GROUPS


class AssignmentError(ValueError):
    """Incomplete or ambiguous assignment cannot yield a supported score."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True)
class CarbonGroup:
    """One required observation and its original-atom/prediction positions."""

    labels: tuple[str, ...]
    atom_indices: tuple[int, ...]
    positions: tuple[int, ...]


@dataclass(frozen=True)
class Assignment:
    """A group-to-observation matching with declared exchangeability."""

    indices: np.ndarray
    objective: float
    exchangeable: bool = False


def build_groups(atom_indices: Sequence[int], equivalent_groups: Sequence[Sequence[str]]) -> tuple[CarbonGroup, ...]:
    """Match reference ordering: explicit groups first, then carbon singletons."""
    labels = {f"C{int(index) + 1}": position for position, index in enumerate(atom_indices)}
    if len(labels) != len(atom_indices) or not labels:
        raise AssignmentError("invalid_carbon_indices", "Carbon atom indices must be unique and nonempty.")
    used: set[str] = set()
    groups: list[CarbonGroup] = []
    for members in equivalent_groups:
        ordered = tuple(sorted(members, key=lambda label: int(label[1:]) if label[1:].isdigit() else -1))
        if (len(ordered) < 2 or len(set(ordered)) != len(ordered)
                or any(label not in labels or label in used for label in ordered)):
            raise AssignmentError("invalid_equivalence_groups", "Equivalence groups must contain disjoint original carbon labels.")
        used.update(ordered)
        groups.append(CarbonGroup(ordered, tuple(int(label[1:]) - 1 for label in ordered),
                                  tuple(labels[label] for label in ordered)))
    for label, position in labels.items():
        if label not in used:
            groups.append(CarbonGroup((label,), (int(label[1:]) - 1,), (position,)))
    if len(groups) > MAX_GROUPS:
        raise AssignmentError("group_limit", f"More than {MAX_GROUPS} carbon groups exceed the bounded assignment domain.")
    return tuple(groups)


def group_predictions(mean, quantiles, groups: Sequence[CarbonGroup]) -> tuple[np.ndarray, np.ndarray]:
    """Average individual outputs after the heads, preserving float32 means."""
    shifts = np.asarray(mean, dtype=np.float32)
    values = np.asarray(quantiles, dtype=np.float32)
    if shifts.ndim != 2 or values.ndim != 3 or values.shape[:2] != shifts.shape:
        raise AssignmentError("invalid_prediction_shape", "Predictions must have conformer, carbon and quantile dimensions.")
    if not np.isfinite(shifts).all() or not np.isfinite(values).all():
        raise AssignmentError("nonfinite_predictions", "Predictions must be finite.")
    try:
        group_means = np.stack([shifts[:, group.positions].mean(axis=1) for group in groups], axis=1)
        group_quantiles = np.stack([values[:, group.positions, :].mean(axis=1) for group in groups], axis=1)
    except (IndexError, ValueError) as exc:
        raise AssignmentError("invalid_group_positions", "Group positions do not match carbon predictions.") from exc
    return group_means, group_quantiles


def _fixed_assignments(group_count: int, fixed: dict[int, int] | None) -> dict[int, int]:
    fixed = dict(fixed or {})
    if (any(type(group) is not int or type(peak) is not int or not 0 <= group < group_count
            or not 0 <= peak < group_count for group, peak in fixed.items())
            or len(set(fixed.values())) != len(fixed)):
        raise AssignmentError("invalid_fixed_assignment", "Fixed assignments must be distinct in-range group/peak indices.")
    return fixed


def squared_shift_cost(predicted: np.ndarray, observed: np.ndarray) -> np.ndarray:
    """Build squared costs, rescaling extreme finite residuals by one common factor."""
    residuals = predicted[:, None] - observed[None, :]
    largest = np.max(np.abs(residuals))
    if np.isfinite(largest) and largest > np.sqrt(np.finfo(float).max / MAX_GROUPS):
        residuals = residuals / largest
    return residuals ** 2


def assign_groups(predicted, observed, *, distributions=None, fixed=None,
                  cancel_check: Callable[[], None] | None = None) -> Assignment:
    """Minimize squared shift error, retaining the solver's first optimal match."""
    if cancel_check is not None:
        cancel_check()
    calculated = np.asarray(predicted, dtype=np.float64)
    experimental = np.asarray(observed, dtype=np.float64)
    if (calculated.ndim != 1 or calculated.shape != experimental.shape or not len(calculated)
            or len(calculated) > MAX_GROUPS or not np.isfinite(calculated).all()
            or not np.isfinite(experimental).all()):
        raise AssignmentError("strict_nmr_coverage_mismatch", "Exactly one finite observation per required carbon group is needed.")
    fixed = _fixed_assignments(len(calculated), fixed)
    indices = np.full(len(calculated), -1, dtype=np.int64)
    for group, peak in fixed.items():
        indices[group] = peak
    rows = np.array([i for i in range(len(calculated)) if i not in fixed], dtype=int)
    columns = np.array([i for i in range(len(experimental)) if i not in fixed.values()], dtype=int)
    exchangeable = False
    if len(rows):
        cost = squared_shift_cost(calculated[rows], experimental[columns])
        matched_rows, matched_columns = linear_sum_assignment(cost)
        indices[rows[matched_rows]] = columns[matched_columns]
        if distributions is not None:
            # Retained only for the exported diagnostic, never as an eligibility gate.
            dist = np.asarray(distributions)
            if dist.shape[0] != len(calculated) or not np.isfinite(dist).all():
                raise AssignmentError("invalid_distributions", "Distributions must follow carbon group order.")
            exchangeable = bool(np.all(calculated[rows] == calculated[rows[0]])
                                and all(np.array_equal(dist[row], dist[rows[0]]) for row in rows))
    if cancel_check is not None:
        cancel_check()
    # Keep the public diagnostic in ppm even though optimization uses squares.
    objective = float(np.abs(calculated - experimental[indices]).sum())
    return Assignment(indices, objective, exchangeable)
