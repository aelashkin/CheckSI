"""Stdlib exhaustive squared-error oracle adapted from G03 experiment v3.

Source SHA256: 2faa892e7cd6593535eaf3367526c0a97a80bb6a77a3e432105a50b798ec35ae
Expected mappings are enumerated without production assignment/scoring imports.
"""

import itertools


def mapping_weights(mapping, observations, capacities, sharing, fixed):
    n = len(mapping)
    if (any(type(k) is not int or type(v) is not int for k, v in fixed.items())
            or any(not 0 <= k < n or not 0 <= v < len(observations) for k, v in fixed.items())):
        raise ValueError("indices")
    if any(type(i) is not int or not 0 <= i < len(observations) for i in mapping):
        raise ValueError("observation index")
    if any(mapping[group] != peak for group, peak in fixed.items()):
        raise ValueError("fixed contradiction")
    weights = [0.] * n
    for peak in range(len(observations)):
        contributors = frozenset(i for i, target in enumerate(mapping) if target == peak)
        capacity = capacities[peak]
        if not contributors:
            raise ValueError("unobserved measurement")
        if capacity is not None:
            if type(capacity) is not int or capacity < 1 or len(contributors) != capacity:
                raise ValueError("exact capacity")
        elif len(contributors) > 1 and contributors not in sharing:
            raise ValueError("unjustified sharing")
        for group in contributors:
            weights[group] = 1. / len(contributors)
    return weights


def exhaustive_reference(predicted, observed, *, capacities=None, sharing=(), fixed=None):
    """Exhaust all tiny-fixture mappings, independent of a production optimizer."""
    n = len(predicted)
    if n > 7 or len(observed) ** n > 100000:
        raise ValueError("private experiment budget")
    capacities = capacities if capacities is not None else [None] * len(observed)
    sharing = {frozenset(group) for group in sharing}
    fixed = fixed or {}
    best = None
    for mapping in itertools.product(range(len(observed)), repeat=n):
        try:
            weights = mapping_weights(mapping, observed, capacities, sharing, fixed)
        except ValueError:
            continue
        objective = sum(w * (predicted[i] - observed[peak]) ** 2
                        for i, (w, peak) in enumerate(zip(weights, mapping)))
        signature = tuple((observed[peak], weights[i]) for i, peak in enumerate(mapping))
        if best is None or objective < best[0] - 1e-12:
            best = objective, mapping, weights, signature, False
        elif abs(objective - best[0]) <= 1e-12 and signature != best[3]:
            best = *best[:4], True
    if best is None:
        raise ValueError("no feasible mapping")
    return {"indices": best[1], "weights": best[2], "objective": best[0], "tied": best[4]}
