"""Find a 13C description whose signals are all displaced by one amount: a referencing error.

DP5q scales the shifts of each compound, so a uniform displacement is not what lowers its
score, and the low-score message cannot name it. The raw difference between the reported and
the predicted shifts shows it: the model has a small offset of its own that is nearly the same
for every compound of a document, and a mis-referenced spectrum stands apart from it by
several ppm while its signals still follow the prediction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median

OFFSET_MIN_PPM = 5.0       # distance of the compound's offset from the offset of the document
RESIDUAL_MAX_PPM = 5.0     # after removing the offset the signals must fit like a correct description
MIN_CARBONS = 6
MIN_COMPOUNDS = 5          # compounds needed to know the offset of the document


@dataclass(frozen=True)
class Offset:
    """Uniform displacement of one description."""

    offset: float          # reported minus predicted, relative to the rest of the document
    residual: float        # root mean square of what remains after removing the compound's offset
    carbons: int


def _own_offset(differences: list[float]) -> tuple[float, float]:
    middle = median(differences)
    return middle, math.sqrt(sum((value - middle) ** 2 for value in differences) / len(differences))


def find_offsets(differences: dict[int, list[float]]) -> dict[int, Offset]:
    """Compounds whose 13C signals are displaced together, keyed like ``differences``.

    ``differences`` gives, per compound, reported minus predicted shift of every matched carbon.
    """
    own = {key: _own_offset(values) for key, values in differences.items() if len(values) >= MIN_CARBONS}
    if len(own) < MIN_COMPOUNDS:
        return {}
    found = {}
    for key, (middle, residual) in own.items():
        others = median(value for other, (value, _) in own.items() if other != key)
        if abs(middle - others) >= OFFSET_MIN_PPM and residual <= RESIDUAL_MAX_PPM:
            found[key] = Offset(middle - others, residual, len(differences[key]))
    return found
