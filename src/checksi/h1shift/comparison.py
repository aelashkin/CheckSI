"""Parse reported 1H signals and compare them with predicted environments."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

import numpy as np
from scipy.optimize import linear_sum_assignment

from .model import UNRELIABLE_ALLOWANCES, ProtonEnvironment

_NUMBER = r'-?\d+(?:[.,]\d+)?'
_SIGNAL = re.compile(r'(?<![\d.])(' + _NUMBER + r')(?:\s*[-–—−]\s*(' + _NUMBER + r'))?\s*'
                     r'\(((?:[^()]|\([^()]*\))*)\)')
_INTEGRAL = re.compile(r'(?<![\w.])(\d+(?:\.\d+)?)\s*[HН](?![a-zA-Z])')
_COUPLING = re.compile(r'J[^=]{0,12}=\s*((?:\d+(?:\.\d+)?\s*(?:,|and)?\s*)+)')
_LETTERS = {'d': 1, 't': 2, 'q': 3, 'p': 4, 'h': 5}
_LONG = {'quint': 'p', 'quin': 'p', 'pent': 'p', 'sext': 'h', 'sept': 'S', 'hept': 'S'}
# Assignment by predicted shift can be wrong (outliers reach 1 ppm): by default a
# multiplicity is reported only when no environment predicted this close could explain it.
# A window of 1.5 ppm was tried on reviewed SI sets: it removed 8 false findings and 4
# confirmed ones. Confirmed findings are not traded for fewer false ones.
ALTERNATIVE_WINDOW_PPM = 1.0


# Distance within which a group of equivalent protons may explain a labelled signal.
COVERAGE_WINDOW_PPM = 1.0
# Steps allowed to the search that matches labelled signals to groups; the best matching found
# so far is used when it runs out.
SEARCH_BUDGET = 20000


def allowance(environment: ProtonEnvironment) -> float:
    """Free distance for a proton of a class the model is known to mispredict, else zero."""
    return UNRELIABLE_ALLOWANCES.get(environment.unreliable, 0.0)


@dataclass(frozen=True)
class ReportedSignal:
    """One reported signal: a shift or range and its integral."""

    low: float
    high: float
    protons: int
    text: str
    multiplicity: str | None = None
    couplings: tuple[float, ...] = ()
    fractional: bool = False

    def partner_counts(self) -> list[int] | None:
        """First-order partners per coupling ('dt' -> [1, 2]); None for s, m, br or app."""
        code = self.multiplicity
        if not code or code == 's':
            return None
        for word, letter in _LONG.items():
            code = code.replace(word, letter)
        counts = [6 if c == 'S' else _LETTERS.get(c) for c in code]
        return None if None in counts else counts


@dataclass(frozen=True)
class Comparison:
    """Optimal one-to-one comparison of predicted protons with reported ones."""

    mean_error: float
    matched: int
    outliers: tuple[tuple[ReportedSignal, float], ...]
    assigned: tuple[tuple[int, ...], ...] = ()
    # Predicted shifts of C-H protons with no reported slot within the outlier threshold.
    unaccounted: tuple[float, ...] = ()
    # Matched protons of classes the model is known to mispredict; they are left out of the
    # mean error and get a wider tolerance in the assignment.
    unreliable: int = 0
    # Reported protons handed to NH/OH/SH protons because no C-H proton fits them.
    exchangeable_slots: int = 0


@dataclass(frozen=True)
class MultiplicityIssue:
    """A reported multiplicity that no nearby predicted environment can produce."""

    signal: ReportedSignal
    predicted: float
    reason: str
    available: int = 0


def parse_signals(section: str) -> list[ReportedSignal] | None:
    """Return reported signals, or None when any signal lacks an integral.

    Non-integer integrals (isomer mixtures such as 0.5H + 0.5H) are rounded up,
    so each such signal can still take a proton; spare slots stay unmatched.
    """
    body = section.split('δ', 1)[1] if 'δ' in section else section
    signals = []
    for match in _SIGNAL.finditer(body):
        first, second, descriptor = match.groups()
        integral = _INTEGRAL.search(descriptor)
        if integral is None:
            return None
        value = float(integral.group(1))
        protons = round(value) if abs(value - round(value)) <= 0.05 else math.ceil(value)
        values = [float(first.replace(',', '.'))] + ([float(second.replace(',', '.'))] if second else [])
        coupling = _COUPLING.search(descriptor)
        couplings = tuple(float(x) for x in re.findall(r'\d+(?:\.\d+)?', coupling.group(1))) if coupling else ()
        signals.append(ReportedSignal(min(values), max(values), protons, match.group(0).strip(),
                                      _multiplicity(descriptor), couplings, protons != value))
    return signals or None


def compare(environments: list[ProtonEnvironment], signals: list[ReportedSignal],
            outlier_ppm: float, exchangeable: int = 0) -> Comparison:
    """Assign protons to reported slots minimising the total distance to each shift or range.

    Protons of an ``unreliable`` class are matched with their class allowance of free
    distance, so they take the signal a correct prediction would have taken, and they do not
    enter the mean error. Beyond the allowance they are reported like any other proton.

    ``exchangeable`` protons (NH, OH, SH) have no predicted shift and take what is left:
    first the reported protons no C-H proton was assigned to, then the reported protons whose
    C-H proton lies beyond ``outlier_ppm``. Such a C-H proton is then one without a signal,
    not a far signal.
    """
    predicted = np.array([env.shift for env in environments for _ in range(env.count)])
    slots = [index for index, signal in enumerate(signals) for _ in range(max(signal.protons, 0))]
    if not len(predicted) or not slots:
        raise ValueError('nothing to compare')
    low = np.array([signals[i].low for i in slots])
    high = np.array([signals[i].high for i in slots])
    cost = np.maximum(np.maximum(low[None] - predicted[:, None], predicted[:, None] - high[None]), 0)
    reliable = np.array([not env.unreliable for env in environments for _ in range(env.count)])
    # Inside the allowance the distance still breaks ties, so such a proton takes its nearest signal.
    ranking = cost.copy()
    free = np.array([allowance(env) for env in environments for _ in range(env.count)])
    cost[~reliable] = np.maximum(cost[~reliable] - free[~reliable, None], 0)
    ranking[~reliable] = cost[~reliable] + 0.01 * ranking[~reliable]
    rows, columns = linear_sum_assignment(ranking)
    errors = cost[rows, columns]
    trusted = reliable[rows]
    # Exchangeable protons not yet covered by leftover reported protons take the worst-fitting ones.
    spare = max(0, exchangeable - max(0, len(slots) - len(rows)))
    handed = set()
    for position in sorted(range(len(rows)), key=lambda p: -errors[p]):
        if len(handed) == spare or errors[position] <= outlier_ppm:
            break
        handed.add(position)
    worst: dict[int, list[float]] = {}
    for position, (row, column, error) in enumerate(zip(rows, columns, errors)):
        if error > outlier_ppm and position not in handed:
            worst.setdefault(slots[column], []).append(float(predicted[row]))
    outliers = tuple((signals[index], float(np.mean(values))) for index, values in sorted(worst.items()))
    owner = [index for index, env in enumerate(environments) for _ in range(env.count)]
    assigned: list[set[int]] = [set() for _ in signals]
    for position, (row, column) in enumerate(zip(rows, columns)):
        if position not in handed:
            assigned[slots[column]].add(owner[row])
    matched_rows = set(int(row) for row in rows)
    unaccounted = sorted([float(predicted[row]) for row, error in zip(rows, errors) if error > outlier_ppm]
                         + [float(value) for index, value in enumerate(predicted) if index not in matched_rows],
                         reverse=True)
    # A C-H proton whose signal went to an exchangeable proton still counts in the mean:
    # a description of another compound must not look better for having an OH.
    mean_error = float(errors[trusted].mean()) if trusted.any() else 0.0
    return Comparison(mean_error, len(errors), outliers, tuple(tuple(sorted(a)) for a in assigned),
                      tuple(unaccounted), int((~trusted).sum()), len(handed))


_AROMATIC_SOLVENT = re.compile(r'C6D6|benzene[- ]?d\s?6|C7D8|toluene[- ]?d\s?8|C5D5N|pyridine[- ]?d\s?5|Py[- ]?d\s?5',
                               re.IGNORECASE)
# Aromatic solvents shift every signal by an amount the model, trained on chloroform-like
# data, does not know; correct descriptions deviate by about 0.45 ppm on average.
AROMATIC_SOLVENT_MEAN_EXTRA_PPM = 0.25
AROMATIC_SOLVENT_SIGNAL_EXTRA_PPM = 0.5


def aromatic_solvent(section: str) -> bool:
    """Whether the header of a 1H section names benzene-d6, toluene-d8 or pyridine-d5."""
    header = section.split('δ', 1)[0] if 'δ' in section else section[:80]
    return bool(_AROMATIC_SOLVENT.search(header.replace('₆', '6').replace('₈', '8').replace('₅', '5')))


def reported_protons(signals: list[ReportedSignal]) -> int:
    """Total reported integral of the description."""
    return sum(max(signal.protons, 0) for signal in signals)


def _multiplicity(descriptor: str) -> str | None:
    """Leading first-order code ('dd', 'quint', 's'); None for m, br, app or anything else."""
    head = descriptor.split(',')[0].strip().lower().replace(' ', '')
    if re.fullmatch(r's|(?:quint|quin|pent|sext|sept|hept)?[dtqph]*', head) and head:
        return head
    return None


def _can_show(environment: ProtonEnvironment, signal: ReportedSignal, exact: bool = True) -> bool:
    """Whether a proton group can give the first-order multiplicity of a signal (any, if none is given).

    The partner counts of an environment are upper bounds. With ``exact`` the label must also
    show at least the couplings that are always resolved (ortho protons of a benzene-type CH).
    """
    counts = signal.partner_counts()
    if counts is None:
        return True
    total = sum(counts)
    large = sum(1 for j in signal.couplings if j >= 5.0)
    needed_near = sum(sorted(counts)[:large]) if len(signal.couplings) == len(counts) else 0
    return (environment.far_partners >= total and environment.near_partners >= needed_near
            and (not exact or total >= environment.min_partners))


def _equivalent_groups(environments: list[ProtonEnvironment], signals: list[ReportedSignal],
                       comparison: Comparison, window: float = 0.0) -> list[tuple[ReportedSignal, int]]:
    """Signals with a first-order label that no single group of equivalent protons can give.

    A doublet, triplet or quartet is the signal of one group of equivalent protons: "d, 6H" is
    an isopropyl group, not six protons of several kinds. Each labelled signal must therefore
    get all its protons from one environment that lies within ``window`` and can show the
    multiplicity, and an environment cannot give more protons than it has. Signals are matched
    to environments so that as many as possible are satisfied, nearest first. A signal of two
    or more protons left without a group is returned with the size of the largest group that
    could show its multiplicity (0 if none). Signals without a first-order label (m, br, s),
    signals of one proton and signals that went to exchangeable protons take no part.
    """
    window = window or COVERAGE_WINDOW_PPM
    # Signals of one proton take no part: a single proton can come from any group, and letting
    # it use up a group of two left the second doublet of a para-substituted ring without one.
    labelled = [index for index, owners in enumerate(comparison.assigned)
                if owners and signals[index].partner_counts() is not None and not signals[index].fractional
                and signals[index].protons >= 2]
    if not labelled:
        return []

    def distance(env: ProtonEnvironment, signal: ReportedSignal) -> float:
        return abs(min(max(env.shift, signal.low), signal.high) - env.shift)

    able = {index: [(distance(env, signals[index]), position) for position, env in enumerate(environments)
                    if distance(env, signals[index]) <= window + allowance(env) and _can_show(env, signals[index])]
            for index in labelled}
    options = {index: sorted((d, position) for d, position in able[index]
                             if environments[position].count >= signals[index].protons) for index in labelled}
    order = sorted(labelled, key=lambda index: (len(options[index]), -signals[index].protons))
    capacity = [env.count for env in environments]
    best: dict = {'score': None, 'choice': {}}
    budget = [SEARCH_BUDGET]

    def search(step: int, choice: dict, missed: int, cost: float) -> None:
        if budget[0] <= 0:
            return
        budget[0] -= 1
        score = (missed, cost)
        if best['score'] is not None and score >= best['score']:
            return
        if step == len(order):
            best['score'], best['choice'] = score, dict(choice)
            return
        index = order[step]
        need = signals[index].protons
        for d, position in options[index]:
            if capacity[position] >= need:
                capacity[position] -= need
                choice[index] = position
                search(step + 1, choice, missed, cost + d)
                capacity[position] += need
                del choice[index]
        search(step + 1, choice, missed + 1, cost)

    search(0, {}, 0, 0.0)
    found = []
    for index in labelled:
        signal = signals[index]
        if index not in best['choice'] and signal.protons >= 2:
            largest = max((environments[position].count for _, position in able[index]), default=0)
            found.append((signal, min(largest, signal.protons)))
    return found


def multiplicity_issues(environments: list[ProtonEnvironment], signals: list[ReportedSignal],
                        comparison: Comparison, window: float = ALTERNATIVE_WINDOW_PPM) -> list[MultiplicityIssue]:
    """Signals whose multiplicity none of the plausible environments can show."""
    issues = []
    for signal, owners in zip(signals, comparison.assigned):
        if not owners:
            continue
        # A group of a mispredicted class can own a signal as far away as its allowance, but only
        # a signal it can fill alone: it must not excuse a larger signal made of other protons.
        def distance(env) -> float:
            return abs(min(max(env.shift, signal.low), signal.high) - env.shift)

        def far_owner(env) -> bool:
            return (bool(env.unreliable) and env.count >= signal.protons
                    and distance(env) <= window + allowance(env))

        nearby = {i for i, env in enumerate(environments) if distance(env) <= window or far_owner(env)}
        assigned = {i for i in owners if not environments[i].unreliable or i in nearby}
        candidates = [environments[i] for i in assigned | nearby]
        if not candidates:
            continue
        predicted = float(np.mean([environments[i].shift for i in owners]))
        if signal.multiplicity == 's' and signal.protons >= 3:
            # Only equivalent groups of 3n protons (methyls) can make up a 3n H singlet.
            # Methyls on one carbon (gem-dimethyl) are topologically equivalent but
            # can be diastereotopic, so each may appear as its own 3H singlet.
            methyls = [env for env in candidates if env.count % 3 == 0
                       and (env.count <= signal.protons or env.geminal_methyls)]
            if methyls and all(env.must_split for env in methyls):
                issues.append(MultiplicityIssue(signal, predicted, 'must_split'))
            continue
        counts = signal.partner_counts()
        if counts is None:
            continue
        total = sum(counts)
        if not any(_can_show(env, signal, exact=False) for env in candidates):
            available = max(env.far_partners if env.far_partners < total else env.near_partners
                            for env in candidates)
            issues.append(MultiplicityIssue(signal, predicted, 'too_many_partners', available))
    reported = {id(issue.signal) for issue in issues}
    for signal, covered in _equivalent_groups(environments, signals, comparison):
        if id(signal) not in reported:
            issues.append(MultiplicityIssue(signal, float(signal.low), 'partly_possible', covered))
    return issues
