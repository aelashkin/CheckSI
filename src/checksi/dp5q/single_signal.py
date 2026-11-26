"""Find one misplaced 13C signal in a description that passes the DP5q score.

DP5q assigns signals to carbons itself. When one shift is wrong by 10-20 ppm the
assignment moves every neighbour by one place, the error is spread over several
carbons as 3-6 ppm each, and the score stays above the threshold. This pass asks
a different question: if one signal and one carbon are left unmatched, does the
rest of the description fit much better? It reuses the predicted shifts of the
finished assessment; no geometry or model work is repeated.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

GAIN_MIN_PPM = 8.0        # root of the squared error removed by leaving one pair unmatched
REST_RMS_MAX_PPM = 3.5     # the remaining signals must then fit like a correct description
MIN_CARBONS = 5
# The model repeats its own errors: the same atom is mispredicted the same way in the
# analogues of one series, while a typo is single. A candidate is dropped when another
# compound of the run leaves the same kind of carbon unmatched by a similar amount.
SERIES_GAIN_MIN_PPM = 5.0
SERIES_TOLERANCE_PPM = 4.0
ENVIRONMENT_RADIUS = 2


@dataclass(frozen=True)
class SingleSignal:
    """The pair left unmatched and what leaving it out changes."""

    experimental: float
    calculated: float
    gain: float
    rest_rms: float
    carbons: int
    environment: str = ''      # the unmatched carbon with its neighbours, as a rooted fragment


def _squared(calculated: list[float], experimental: list[float]) -> float:
    return sum((c - e) ** 2 for c, e in zip(calculated, experimental))


def carbon_environment(mol, atom_index: int, radius: int = ENVIRONMENT_RADIUS) -> str:
    """Canonical fragment of one atom and everything within ``radius`` bonds, rooted at the atom."""
    from rdkit import Chem

    distances = Chem.GetDistanceMatrix(mol)[atom_index]
    atoms = [atom.GetIdx() for atom in mol.GetAtoms() if distances[atom.GetIdx()] <= radius]
    return Chem.MolFragmentToSmiles(mol, atomsToUse=atoms, rootedAtAtom=atom_index, canonical=True)


def single_signal(calculated: list[float], experimental: list[float],
                  environments: list[str] | None = None) -> SingleSignal | None:
    """Best single unmatched pair of a one-to-one comparison in shift order, or None.

    For sorted lists the order-preserving pairing is the least-squares assignment, so
    every choice of one carbon and one signal to leave out is scored exactly.
    ``environments`` names each carbon of ``calculated`` for the series comparison.
    """
    order = sorted(range(len(calculated)), key=lambda index: calculated[index])
    names = [environments[index] for index in order] if environments else None
    calculated, experimental = sorted(calculated), sorted(experimental)
    count = len(calculated)
    if count != len(experimental) or count < MIN_CARBONS:
        return None
    full = _squared(calculated, experimental)
    best = (full, -1, -1)
    for i in range(count):
        without_carbon = calculated[:i] + calculated[i + 1:]
        for j in range(count):
            rest = _squared(without_carbon, experimental[:j] + experimental[j + 1:])
            if rest < best[0]:
                best = (rest, i, j)
    rest, i, j = best
    if i < 0:
        return None
    return SingleSignal(experimental[j], calculated[i], math.sqrt(max(full - rest, 0.0)),
                        math.sqrt(rest / (count - 1)), count, names[i] if names else '')


def misplaced_signals(candidates: dict) -> dict:
    """Keep the candidates that are large, isolated and not repeated in a series.

    ``candidates`` maps any compound key to its ``SingleSignal`` (or None).
    """
    found = {}
    for key, item in candidates.items():
        if item is None or item.gain < GAIN_MIN_PPM or item.rest_rms > REST_RMS_MAX_PPM:
            continue
        repeated = bool(item.environment) and any(
            other is not None and other_key != key and other.gain >= SERIES_GAIN_MIN_PPM
            and other.environment == item.environment
            and abs((other.experimental - other.calculated) - (item.experimental - item.calculated))
            <= SERIES_TOLERANCE_PPM
            for other_key, other in candidates.items())
        if not repeated:
            found[key] = item
    return found
