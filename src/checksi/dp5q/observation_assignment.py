"""Bounded full-environment assignment to original measured observations."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Callable

import numpy as np
from scipy.optimize import linear_sum_assignment

from .assignment import AssignmentError, assign_groups, squared_shift_cost
from .contracts import MAX_GROUPS

MAX_POLICIES = 64
MAX_POLICY_STATES = MAX_POLICIES * MAX_GROUPS


@dataclass(frozen=True)
class ObservationAssignment:
    """One original observation and evidence weight per complete environment."""

    indices: np.ndarray
    evidence_weights: np.ndarray
    objective: float
    coincident_observations: dict[int, tuple[int, ...]]
    exchangeable: bool = False


def _indices(values, count, *, minimum=0):
    if (not isinstance(values, (tuple, list)) or len(values) < minimum
            or any(type(i) is not int or not 0 <= i < count for i in values)
            or len(set(values)) != len(values)):
        raise AssignmentError('invalid_observation_constraints', 'Environment indices must be unique bounded integers.')
    return tuple(values)


def _policies(count, capacities, unknown, coalitions, fixed, cancel):
    """Enumerate count-feasible policies, pruning coalition subsets by exact sums."""
    coalitions = tuple(group for group in coalitions
                       if len({fixed[index] for index in group if index in fixed}) <= 1)
    extras = tuple(len(group) - 1 for group in coalitions)
    states = 0

    def poll():
        nonlocal states
        cancel()
        states += 1
        if states > MAX_POLICY_STATES:
            raise AssignmentError('assignment_search_limit', 'Policy-generation work exceeds the bounded search domain.')

    @lru_cache(None)
    def possible(start, size, total):
        poll()
        if size == 0:
            return total == 0
        if size < 0 or total < size or len(extras) - start < size:
            return False
        remaining = sorted(extras[start:])
        if total < sum(remaining[:size]) or total > sum(remaining[-size:]):
            return False
        return (possible(start + 1, size - 1, total - extras[start])
                or possible(start + 1, size, total))

    def subsets(start, size, total, chosen=()):
        poll()
        if size == 0:
            if total == 0:
                yield chosen
            return
        for index in range(start, len(extras) - size + 1):
            if possible(index + 1, size - 1, total - extras[index]):
                yield from subsets(index + 1, size - 1, total - extras[index], chosen + (index,))

    def destinations(selected, chosen=()):
        poll()
        if len(chosen) == len(selected):
            yield chosen
            return
        group = coalitions[selected[len(chosen)]]
        required = {fixed[index] for index in group if index in fixed}
        choices = tuple(required) if required else unknown
        for peak in choices:
            if (peak not in unknown or peak in chosen
                    or any(value == peak and index not in group for index, value in fixed.items())):
                continue
            yield from destinations(selected, chosen + (peak,))

    missing = count - sum(capacities)
    for coalition_count in range(min(len(coalitions), len(unknown), missing) + 1):
        if not possible(0, coalition_count, missing):
            continue
        for selected in subsets(0, coalition_count, missing):
            for observations in destinations(selected):
                policy_capacities = list(capacities)
                forced = {}
                for index, observation in zip(selected, observations):
                    policy_capacities[observation] = len(coalitions[index])
                    forced.update((group, observation) for group in coalitions[index])
                yield tuple(policy_capacities), forced


def _constraints(count, observed_count, observation_atom_counts, coincident_environments, fixed):
    if (type(count) is not int or type(observed_count) is not int
            or not 1 <= observed_count <= count <= MAX_GROUPS):
        raise AssignmentError('strict_nmr_coverage_mismatch', 'Full environment and observation counts are outside the bounded domain.')
    annotations = [None] * observed_count if observation_atom_counts is None else observation_atom_counts
    if (not isinstance(annotations, (tuple, list)) or len(annotations) != observed_count
            or any(value is not None and (type(value) is not int or not 1 <= value <= MAX_GROUPS)
                   for value in annotations)):
        raise AssignmentError('annotation_partition_mismatch', 'Observation capacities must be aligned positive integers or unknown.')
    capacities = tuple(value if value is not None else 1 for value in annotations)
    if not isinstance(coincident_environments, (tuple, list)):
        raise AssignmentError('invalid_observation_constraints', 'Coincidence coalitions must be a sequence.')
    coalitions = tuple(_indices(group, count, minimum=2) for group in coincident_environments)
    used = [index for group in coalitions for index in group]
    if len(used) != len(set(used)):
        raise AssignmentError('invalid_observation_constraints', 'Supplied coincidence coalitions must not overlap.')
    if fixed is not None and not isinstance(fixed, dict):
        raise AssignmentError('invalid_fixed_assignment', 'Fixed assignments must be an integer mapping.')
    fixed = dict(fixed or {})
    if any(type(group) is not int or type(peak) is not int
           or not 0 <= group < count or not 0 <= peak < observed_count for group, peak in fixed.items()):
        raise AssignmentError('invalid_fixed_assignment', 'Fixed indices must be bounded integers, not booleans or strings.')
    return annotations, capacities, coalitions, fixed


def _feasible_policies(count, observed_count, annotations, capacities, coalitions, fixed, cancel):
    policies = []
    unknown = tuple(index for index, value in enumerate(annotations) if value is None)
    for policy_capacities, forced in _policies(count, capacities, unknown, coalitions, fixed, cancel):
        if any(group in forced and forced[group] != peak for group, peak in fixed.items()):
            continue
        forced = {**forced, **fixed}
        if any(sum(value == peak for value in forced.values()) > policy_capacities[peak]
               for peak in range(observed_count)):
            continue
        policies.append((policy_capacities, forced))
        if len(policies) > MAX_POLICIES:
            raise AssignmentError('assignment_search_limit', 'More than 64 complete observation policies require manual review.')
    return policies


def validate_observation_coverage(count, observed_count, *, observation_atom_counts=None,
                                  coincident_environments=(), fixed=None, cancel_check=None) -> None:
    """Prove non-boron product coverage without predictions or optimization."""
    cancel = cancel_check or (lambda: None)
    cancel()
    values = _constraints(count, observed_count, observation_atom_counts, coincident_environments, fixed)
    if not _feasible_policies(count, observed_count, *values, cancel):
        reason = ('annotation_partition_mismatch' if observation_atom_counts is not None
                  and any(value is not None for value in observation_atom_counts) else 'strict_nmr_coverage_mismatch')
        raise AssignmentError(reason, 'No complete observation policy satisfies counts, capacities and fixed assignments.')


def assign_observations(predicted, observed, *, observation_atom_counts=None,
                        coincident_environments=(),
                        fixed=None, distributions=None,
                        cancel_check: Callable[[], None] | None = None) -> ObservationAssignment:
    """Assign distinct predictions without averaging or duplicating measurements.

    Callers separately prove structural coalition eligibility. The kernel covers
    every environment, retaining the first squared-error
    optimum across at most 64 policies and 8192 policy-generation states.
    """
    cancel = cancel_check or (lambda: None)
    cancel()
    calculated = np.asarray(predicted, dtype=np.float64)
    experimental = np.asarray(observed, dtype=np.float64)
    if (calculated.ndim != 1 or experimental.ndim != 1
            or not 1 <= len(experimental) <= len(calculated) <= MAX_GROUPS
            or not np.isfinite(calculated).all() or not np.isfinite(experimental).all()):
        raise AssignmentError('strict_nmr_coverage_mismatch', 'Finite full environments and original observations are required.')
    count, observed_count = len(calculated), len(experimental)
    annotations, capacities, coalitions, fixed = _constraints(
        count, observed_count, observation_atom_counts, coincident_environments, fixed)
    if count == observed_count and all(capacity == 1 for capacity in capacities):
        legacy = assign_groups(calculated, experimental, distributions=distributions,
                               fixed=fixed, cancel_check=cancel)
        return ObservationAssignment(legacy.indices, np.ones(count), legacy.objective, {}, legacy.exchangeable)

    policies = _feasible_policies(count, observed_count, annotations, capacities, coalitions, fixed, cancel)
    best = None
    best_cost = None
    costs = squared_shift_cost(calculated, experimental)
    for policy_capacities, forced in policies:
        slots = np.repeat(np.arange(observed_count), policy_capacities)
        slot_weights = 1. / np.asarray(policy_capacities)[slots]
        cost = costs[:, slots] * slot_weights
        for group, peak in forced.items():
            cost[group, slots != peak] = np.inf
        cancel()
        matched_rows, columns = linear_sum_assignment(cost)
        squared_cost = float(cost[matched_rows, columns].sum())
        indices = slots[columns]
        weights = slot_weights[columns]
        objective = float((weights * np.abs(calculated - experimental[indices])).sum())
        shared = {peak: tuple(np.flatnonzero(indices == peak).tolist()) for peak in range(observed_count)
                  if np.count_nonzero(indices == peak) > 1}
        candidate = ObservationAssignment(indices, weights, objective, shared)
        if best is None or squared_cost < best_cost:
            best, best_cost = candidate, squared_cost
    if best is None:
        raise AssignmentError('strict_nmr_coverage_mismatch', 'No complete observation policy satisfies capacities and fixed assignments.')
    return best
