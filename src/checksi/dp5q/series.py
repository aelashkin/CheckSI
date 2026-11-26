"""Series of compounds in which DP5q misses the same carbon by the same amount.

The same atom deviating alike in several analogues of one file has two possible
causes that the data cannot tell apart: the model mispredicts this fragment, or the
structure of the whole series is wrong. Nothing is suppressed here; the series is
named so that the reader sees one question instead of many separate warnings. Whether
independent proof such as an X-ray structure exists is left to the reader: finding it
in free text is a matter for review, not for a rule.
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_ERROR_PPM = 5.0        # scaled error of the carbon in every member
SPREAD_PPM = 3.0           # the errors of the members lie within this window
MIN_COMPOUNDS = 3


@dataclass(frozen=True)
class Series:
    """One carbon environment and the compounds in which it deviates alike."""

    environment: str
    members: tuple          # compound keys, in input order
    mean_error: float


def find_series(deviations: dict) -> list[Series]:
    """Group compounds by a shared deviating carbon.

    ``deviations`` maps a compound key to ``[(environment, signed error), ...]`` of its
    assessed carbons. For every environment the largest set of compounds whose errors of
    at least ``MIN_ERROR_PPM`` lie within ``SPREAD_PPM`` of each other forms a series.
    """
    by_environment: dict[str, list] = {}
    for key, rows in deviations.items():
        for environment, error in rows:
            if abs(error) >= MIN_ERROR_PPM:
                by_environment.setdefault(environment, []).append((error, key))
    order = {key: position for position, key in enumerate(deviations)}
    found = []
    for environment, rows in by_environment.items():
        rows.sort(key=lambda row: row[0])
        best: list = []
        for start in rows:
            group = [row for row in rows if 0 <= row[0] - start[0] <= SPREAD_PPM]
            if len({key for _, key in group}) > len({key for _, key in best}):
                best = group
        members = tuple(sorted({key for _, key in best}, key=order.get))
        if len(members) >= MIN_COMPOUNDS:
            found.append(Series(environment, members, sum(error for error, _ in best) / len(best)))
    return found
