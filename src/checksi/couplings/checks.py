"""Rules for coupling constants in 1H and 13C descriptions.

All rules are deliberately one-sided: they report what cannot be right for the
structure (or for any structure), not what merely looks unusual.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rdkit import Chem

from .parsing import Signal

# Elements with abundant spin-1/2 isotopes other than F and P, or NMR-active metals:
# their couplings can appear in either spectrum, so value rules stay silent.
_OTHER_COUPLING_ELEMENTS = frozenset({'Sn', 'Pt', 'Hg', 'Se', 'Te', 'Tl', 'Pb', 'Rh', 'Ag', 'W', 'Y', 'Cd', 'Si', 'B', 'D', 'T'})
PROTON_SHIFT_MAX = 20.0
AVERAGED_TOLERANCE = 0.5        # a t/q/p constant is the mean of unequal couplings to its partners
MISMATCH_MAX = 1.0              # two unpaired constants this close are one coupling measured twice
PAIRING_MIN = 3.0               # smaller long-range couplings are often unresolved at the partner
HH_ABSOLUTE_MAX = 20.5          # geminal sp3 H-H, the largest proton-proton coupling
GEMINAL_HF_MIN = 40.0           # 2J(H,F): 44-60 Hz on sp3 carbon, 70-85 Hz on a vinyl carbon
DIRECT_PH_MIN = 150.0           # 1J(P,H)
# J(C,F) ranges by number of bonds; between 60 and 150 Hz there is no C-F coupling.
CF_ONE_BOND = (150.0, 400.0)
CF_TWO_BOND = (12.0, 60.0)
CF_LONG_RANGE = (1.0, 12.0)


@dataclass(frozen=True)
class Issue:
    """One finding: an error-code key, a template variant and its context."""

    code: str
    variant: str = 'default'
    context: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StructureFacts:
    """What the structure allows; built once per compound."""

    elements: frozenset
    hh_max: float
    carbon_bound_h: int
    geminal_hf: bool
    vicinal_hf: bool
    direct_ph: bool
    cf_one: tuple[int, ...]      # number of attached F for each carbon environment bearing F
    cf_two: tuple[int, ...]      # number of F two bonds away for each such environment
    cf_three_aromatic: int       # aromatic carbon environments three bonds from F
    other_nuclei: bool
    hh_windows: tuple = ()       # shift windows (ppm) of every pair of coupled proton positions, both ways
    cf_windows: tuple = ()       # shift windows (ppm) of carbons bonded to one fluorine
    ch_windows: tuple = ()       # like hh_windows, for protons on carbon only
    cf_two_large: int = 0        # two-bond environments whose 2J cannot be mistaken for a long-range one


_HETERO_NEIGHBOURS = {7, 8, 9, 15, 16, 17, 35, 53}


def _shift_window(atom) -> tuple[float, float]:
    """Generous range of 1H shifts for the protons on this atom, by the kind of position only.

    The ranges decide whether an unresolved signal could contain a coupling partner, so they err on the
    wide side: a window that is too wide only keeps the pairing rule silent.
    """
    if atom.GetAtomicNum() != 6:
        return (0.3, 14.5)                       # NH, OH, SH
    if atom.GetIsAromatic():
        return (5.6, 10.0)
    hybridization = atom.GetHybridization()
    if hybridization == Chem.HybridizationType.SP2:
        carbonyl = any(b.GetBondTypeAsDouble() == 2 and b.GetOtherAtom(atom).GetAtomicNum() == 8
                       for b in atom.GetBonds())
        return (8.8, 10.9) if carbonyl else (4.2, 8.4)
    if hybridization == Chem.HybridizationType.SP:
        return (1.6, 3.6)
    if any(n.GetAtomicNum() in _HETERO_NEIGHBOURS for n in atom.GetNeighbors()):
        return (1.9, 6.8)
    return (-0.6, 4.6)


def _coupled_windows(mol, carbon_only: bool = False) -> tuple:
    """Shift windows of every geminal and vicinal pair of proton positions, in both directions.

    ``carbon_only`` leaves out OH, NH and SH protons: their window covers the whole spectrum, and a
    pair that exists only through one of them says nothing about two given signals.
    """
    pairs = set()
    for atom in mol.GetAtoms():
        hydrogens = atom.GetTotalNumHs()
        if not hydrogens or (carbon_only and atom.GetAtomicNum() != 6):
            continue
        own = _shift_window(atom)
        if hydrogens >= 2 and atom.GetHybridization() == Chem.HybridizationType.SP3:
            pairs.add((own, own))                # diastereotopic protons of one CH2
        for neighbour in atom.GetNeighbors():
            if neighbour.GetTotalNumHs() and not (carbon_only and neighbour.GetAtomicNum() != 6):
                pairs.add((own, _shift_window(neighbour)))
    return tuple(sorted(pairs))


def _resolved_three_bond(mol, distance, carbon: int, fluorines: list[int]) -> bool:
    """Whether a 3J(C,F) to an aromatic carbon is large enough to be expected in the list.

    Meta carbons of an aryl fluoride (about 8 Hz) and ortho carbons of an aryl-CF3 or aryl-CF2
    group (3-6 Hz) are routinely resolved. From a single fluorine on an sp3 carbon, or through
    oxygen (OCF3), the coupling is 1-3 Hz and often not resolved, so it is not required.
    """
    for fluorine in fluorines:
        if distance[carbon][fluorine] != 3:
            continue
        bearer = mol.GetAtomWithIdx(fluorine).GetNeighbors()[0]
        if bearer.GetIsAromatic():
            return True
        attached = sum(1 for n in bearer.GetNeighbors() if n.GetSymbol() == 'F')
        if attached >= 2 and any(n.GetIsAromatic() and distance[n.GetIdx()][carbon] == 1 for n in bearer.GetNeighbors()):
            return True
    return False


def _fluorinated_carbon_windows(mol) -> tuple:
    """Shift windows (ppm) in which the carbons bonded to a single fluorine resonate."""
    windows = set()
    for atom in mol.GetAtoms():
        if atom.GetAtomicNum() != 6 or sum(1 for n in atom.GetNeighbors() if n.GetSymbol() == 'F') != 1:
            continue
        if atom.GetIsAromatic():
            windows.add((150.0, 172.0))
        elif atom.GetHybridization() == Chem.HybridizationType.SP2:
            windows.add((140.0, 175.0))
        else:
            windows.add((70.0, 110.0))
    return tuple(sorted(windows))


def structure_facts(smiles: str) -> StructureFacts | None:
    """Coupling-relevant facts of a structure, or None when it cannot be parsed."""
    mol = Chem.MolFromSmiles(smiles) if smiles else None
    if mol is None:
        return None
    elements = frozenset(atom.GetSymbol() for atom in mol.GetAtoms())
    isotopes = any(atom.GetIsotope() for atom in mol.GetAtoms())
    hh_max = 0.0
    for atom in mol.GetAtoms():
        hydrogens = atom.GetTotalNumHs()
        if atom.GetAtomicNum() != 6 or not hydrogens:
            continue
        if hydrogens == 2:   # geminal: 19 Hz on sp3 carbon, 3 Hz on =CH2
            hh_max = max(hh_max, 19.0 if atom.GetHybridization() == Chem.HybridizationType.SP3 else 3.5)
        for bond in atom.GetBonds():
            other = bond.GetOtherAtom(atom)
            if not other.GetTotalNumHs():
                continue
            if other.GetAtomicNum() != 6:      # H-C-N-H, H-C-O-H
                hh_max = max(hh_max, 12.0)
            elif bond.GetIsAromatic():
                hh_max = max(hh_max, 9.5 if min(len(r) for r in mol.GetRingInfo().AtomRings()
                                                 if atom.GetIdx() in r and other.GetIdx() in r) >= 6 else 6.0)
            elif bond.GetBondTypeAsDouble() == 2:
                hh_max = max(hh_max, 12.5 if bond.IsInRing() else 18.5)
            else:
                sp3 = (atom.GetHybridization() == Chem.HybridizationType.SP3
                       and other.GetHybridization() == Chem.HybridizationType.SP3)
                hh_max = max(hh_max, 14.0 if sp3 else 12.0)
    fluorines = [atom.GetIdx() for atom in mol.GetAtoms() if atom.GetSymbol() == 'F']
    geminal_hf = any(n.GetAtomicNum() == 6 and n.GetTotalNumHs()
                     for f in fluorines for n in mol.GetAtomWithIdx(f).GetNeighbors())
    vicinal_hf = any(m.GetTotalNumHs() for f in fluorines for n in mol.GetAtomWithIdx(f).GetNeighbors()
                     for m in n.GetNeighbors() if m.GetAtomicNum() == 6)
    direct_ph = any(atom.GetSymbol() == 'P' and atom.GetTotalNumHs() for atom in mol.GetAtoms())
    one, two, three, two_large = {}, {}, set(), set()
    if fluorines:
        distance = Chem.GetDistanceMatrix(mol)
        # Enantiotopic carbons (the two arms of a meso compound) give one signal, so the environments are
        # counted without stereo labels. This is a lower bound: a completeness rule must not ask for more
        # signals than the least the structure can show.
        flat = Chem.Mol(mol)
        Chem.RemoveStereochemistry(flat)
        ranks = list(Chem.CanonicalRankAtoms(flat, breakTies=False))
        for atom in mol.GetAtoms():
            if atom.GetAtomicNum() != 6:
                continue
            index = atom.GetIdx()
            nearest = min(distance[index][f] for f in fluorines)
            count = sum(1 for f in fluorines if distance[index][f] == nearest)
            if nearest == 1:
                one[ranks[index]] = count
            elif nearest == 2:
                two[ranks[index]] = count
                # A 2J is not always large: next to a vinyl fluoride, and for a substituted carbon ortho
                # to an aryl fluoride (8-14 Hz), it can look like a long-range coupling. Elsewhere it is
                # 17 Hz or more and its absence is not a matter of resolution.
                bearers = [mol.GetAtomWithIdx(f).GetNeighbors()[0] for f in fluorines if distance[index][f] == 2]
                vinyl = any(b.GetHybridization() == Chem.HybridizationType.SP2 and not b.GetIsAromatic()
                            for b in bearers)
                aryl = any(b.GetIsAromatic() for b in bearers)
                if not vinyl and not (aryl and not atom.GetTotalNumHs()):
                    two_large.add(ranks[index])
            elif nearest == 3 and atom.GetIsAromatic() and _resolved_three_bond(mol, distance, index, fluorines):
                three.add(ranks[index])
    carbon_bound_h = sum(atom.GetTotalNumHs() for atom in mol.GetAtoms() if atom.GetAtomicNum() == 6)
    return StructureFacts(elements, hh_max, carbon_bound_h, geminal_hf, vicinal_hf, direct_ph,
                          tuple(sorted(one.values())), tuple(sorted(two.values())), len(three),
                          bool(elements & _OTHER_COUPLING_ELEMENTS) or isotopes, _coupled_windows(mol),
                          _fluorinated_carbon_windows(mol), _coupled_windows(mol, carbon_only=True), len(two_large))


def _averaged_constants(signal: Signal) -> list:
    """Constants of a signal with a t, q or p part: one value may stand for two or more partners.

    Authors list constants by size, not in the order of the letters, so every
    constant of such a signal is treated as possibly averaged.
    """
    order = signal.first_order
    return list(signal.couplings) if order and set(order) & set('tqphS') else []


PAIRING_TOLERANCE = 0.5         # two constants read independently at 0.12-0.24 Hz per point


def _tolerance(signals: list[Signal]) -> float:
    """Larger of ``PAIRING_TOLERANCE`` and the reporting step of the description.

    A constant is the difference of two peak positions, each known to about one digital
    step; two partners read independently can therefore differ by 0.25-0.5 Hz on a routine
    300-400 MHz spectrum without anything being wrong.
    """
    constants = [c for s in signals for c in s.couplings]
    if constants and all(c.decimals == 0 for c in constants):
        return 1.0
    return PAIRING_TOLERANCE


def _may_hide_partner(signal: Signal, signals: list[Signal], facts: StructureFacts | None) -> bool:
    """Whether some multiplet or broad signal could contain the coupling partner of this signal.

    Without a structure any unresolved signal can. With one, only an unresolved signal that lies where a
    proton coupled to a proton of this signal's region can resonate: an aromatic multiplet does not hide the
    partner of an aliphatic doublet.
    """
    unresolved = [s for s in signals if s is not signal and s.unresolved]
    if not unresolved:
        return False
    if facts is None:
        return True

    def overlaps(s: Signal, window: tuple[float, float]) -> bool:
        return s.low <= window[1] and s.high >= window[0]

    return any(overlaps(signal, own) and overlaps(other, partner)
               for own, partner in facts.hh_windows for other in unresolved)


def proton_issues(signals: list[Signal], facts: StructureFacts | None, *, has_fluorine_or_phosphorus: bool,
                  fluorine_section: str | None = None) -> list[Issue]:
    """Pairing, plausibility and heteronuclear checks of a 1H description."""
    issues: list[Issue] = []
    signals = [s for s in signals if s.high <= PROTON_SHIFT_MAX]   # a 13C line that slipped into the 1H text
    hetero = has_fluorine_or_phosphorus or bool(facts and facts.elements & {'F', 'P'})
    silent = bool(facts and facts.other_nuclei)
    proton_proton = [(s, c) for s in signals for c in s.couplings if not c.heteronuclear]

    # 1. Every H-H coupling appears in two signals. An F/P atom can hide the partner, and so can a multiplet
    #    that lies where the partner can resonate.
    reported = sum(s.protons or 0 for s in signals)
    complete = facts is None or reported >= facts.carbon_bound_h
    if not hetero and not silent and complete:
        tolerance = _tolerance(signals)
        averaged = {id(c) for s in signals for c in _averaged_constants(s)}
        hidden = {id(s): _may_hide_partner(s, signals, facts) for s in signals}
        unpaired = []
        for signal, coupling in proton_proton:
            if coupling.value < PAIRING_MIN or hidden[id(signal)]:
                continue
            partners = [c for other in signals if other is not signal for c in other.couplings
                        if abs(c.value - coupling.value) <= (max(tolerance, AVERAGED_TOLERANCE)
                                                             if id(c) in averaged or id(coupling) in averaged
                                                             else tolerance) + 1e-9]
            if not partners:
                unpaired.append((signal, coupling))
        # Two unpaired constants of different signals within MISMATCH_MAX are most likely the two
        # ends of one coupling read with different values: a milder finding than a missing partner.
        # A constant that is itself too large for an H-H coupling is never excused this way.
        limit = min(HH_ABSOLUTE_MAX, facts.hh_max + 0.5) if facts is not None and facts.hh_max else HH_ABSOLUTE_MAX
        def can_couple(first: Signal, second: Signal) -> bool:
            # With a structure, two signals are a pair only where two coupled protons can resonate:
            # an aromatic doublet and a chain CH2 with similar constants are not partners.
            if facts is None:
                return True
            return any(first.low <= own[1] and first.high >= own[0] and second.low <= partner[1]
                       and second.high >= partner[0] for own, partner in facts.ch_windows)

        def close(signal, coupling) -> list:
            return [(other, c) for other, c in unpaired if other is not signal
                    and abs(c.value - coupling.value) <= MISMATCH_MAX + 1e-9 and can_couple(signal, other)]

        reported = set()
        for signal, coupling in unpaired:
            near = close(signal, coupling)
            # Only an unambiguous pair counts: each constant has the other as its single neighbour.
            if (len(near) == 1 and len(close(*near[0])) == 1 and coupling.value <= limit
                    and near[0][1].value <= limit):
                other, partner = near[0]
                if id(partner) in reported:
                    continue                      # one finding per pair, raised at its first signal
                reported.add(id(coupling))
                difference = abs(partner.value - coupling.value)
                issues.append(Issue('hnmr_j_partner_mismatch', context={
                    'signal': signal.text, 'value': f'{coupling.value:g}', 'other': other.text,
                    'other_value': f'{partner.value:g}', 'difference': f'{difference:.1f}',
                    'tolerance': f'{tolerance:g}'}))
            else:
                issues.append(Issue('hnmr_j_partner_missing', context={
                    'signal': signal.text, 'value': f'{coupling.value:g}', 'tolerance': f'{tolerance:g}'}))

    # 2. Size: no H-H coupling exceeds 20.5 Hz, and none exceeds what this structure can show.
    # Without a resolved structure nothing is known about F or P in the compound: the finding
    # stays, but its text does not claim their absence.
    if not hetero and not silent:
        for signal, coupling in proton_proton:
            if coupling.value > HH_ABSOLUTE_MAX:
                issues.append(Issue('hnmr_j_value_implausible', 'absolute' if facts is not None else 'no_structure', {
                    'signal': signal.text, 'value': f'{coupling.value:g}', 'limit': f'{HH_ABSOLUTE_MAX:g}'}))
            elif facts is not None and facts.hh_max and coupling.value > facts.hh_max + 0.5:
                issues.append(Issue('hnmr_j_value_implausible', 'structure', {
                    'signal': signal.text, 'value': f'{coupling.value:g}', 'limit': f'{facts.hh_max:g}'}))

    # 3. Couplings to F and P that the structure requires.
    if facts is not None and not silent:
        largest = max((c.value for s in signals for c in s.couplings), default=0.0)
        if facts.geminal_hf and largest < GEMINAL_HF_MIN:
            issues.append(Issue('hnmr_heteronuclear_coupling_missing', 'geminal_hf',
                                {'limit': f'{GEMINAL_HF_MIN:g}'}))
        if facts.direct_ph and largest < DIRECT_PH_MIN:
            issues.append(Issue('hnmr_heteronuclear_coupling_missing', 'direct_ph',
                                {'limit': f'{DIRECT_PH_MIN:g}'}))
        if (facts.geminal_hf or facts.vicinal_hf) and fluorine_section and not _decoupled(fluorine_section, '1H'):
            from .parsing import parse_signals
            singlets = [s.text for s in parse_signals(fluorine_section) if s.first_order == 's']
            if singlets:
                issues.append(Issue('fnmr_decoupling_not_stated', 'default',
                                    {'signal': ', '.join(singlets[:3])}))
    return issues


def _decoupled(section: str, nucleus: str) -> bool:
    """'{1H}', '19F-decoupled' and similar marks in an NMR section header or text."""
    pattern = r'\{[^}]*' + re.escape(nucleus) + r'[^}]*\}|' + re.escape(nucleus) + r'[- ]?decoupl'
    return bool(re.search(pattern, section, re.IGNORECASE))


def _letter(count: int) -> str:
    return {1: 'd', 2: 't', 3: 'q'}.get(count, '')


def _fits(order: str, fluorines: set[int]) -> bool:
    """Whether a first-order code can come from one of the given numbers of fluorine atoms.

    Equivalent fluorines give d, t or q. The two fluorines of a CF2 group are often not
    equivalent (=CF2 of a gem-difluoroalkene, CF2 next to a stereocentre) and then give dd.
    The three fluorines of CF3 are always equivalent: only q is accepted for them.
    """
    partners = sum({'d': 1, 't': 2, 'q': 3}.get(letter, 99) for letter in order)
    return bool(set(order) & {_letter(n) for n in fluorines}) or (partners == 2 and 2 in fluorines)


def _separate_lines(section: str, facts: StructureFacts) -> tuple[float, float, float] | None:
    """Two plain signals that are the two lines of one C-F doublet: (low, high, spacing in Hz).

    Looked for only where a carbon bonded to one fluorine resonates, and only between signals
    listed without a descriptor: "163.3, 160.9" at 101 MHz is 242 Hz apart.
    """
    from .parsing import bare_shifts, spectrometer_frequency
    frequency = spectrometer_frequency(section)
    if not frequency or not facts.cf_windows:
        return None
    shifts = sorted(set(bare_shifts(section)))
    for low, high in facts.cf_windows:
        inside = [value for value in shifts if low <= value <= high]
        # Not only neighbours: another signal (a C=O) can lie between the two lines.
        for position, first in enumerate(inside):
            for second in inside[position + 1:]:
                spacing = (second - first) * frequency
                if CF_ONE_BOND[0] <= spacing <= 300.0:
                    return first, second, spacing
    return None


def carbon_issues(signals: list[Signal], facts: StructureFacts | None, section: str = '') -> list[Issue]:
    """Size, multiplicity and completeness of C-F couplings in a 13C description."""
    if facts is None or facts.other_nuclei or _decoupled(section, '19F'):
        return []
    issues: list[Issue] = []
    split = [s for s in signals if s.couplings]
    has_f, has_p = 'F' in facts.elements, 'P' in facts.elements
    if not has_f and not has_p:
        for signal in split:
            issues.append(Issue('c13_cf_j_value_implausible', 'no_partner', {'signal': signal.text}))
        return issues
    if not has_f:
        return issues
    one = two = three = 0
    for signal in split:
        largest = max(c.value for c in signal.couplings)
        order = signal.first_order
        # Every constant of the signal, not only the first or the largest, must be a possible C-F coupling.
        if not has_p:
            for coupling in signal.couplings:
                if CF_TWO_BOND[1] < coupling.value < CF_ONE_BOND[0] or coupling.value > CF_ONE_BOND[1]:
                    issues.append(Issue('c13_cf_j_value_implausible', 'range',
                                        {'signal': signal.text, 'value': f'{coupling.value:g}'}))
        if largest >= CF_ONE_BOND[0]:
            one += 1
            expected = {_letter(n) for n in facts.cf_one}
            if not facts.cf_one and not has_p:
                issues.append(Issue('c13_cf_j_value_implausible', 'no_cf_bond',
                                    {'signal': signal.text, 'value': f'{largest:g}'}))
            elif (largest <= CF_ONE_BOND[1] and facts.cf_one and order and order != 's' and not has_p
                  and not _fits(order, set(facts.cf_one))):
                issues.append(Issue('c13_cf_multiplicity_mismatch', context={
                    'signal': signal.text, 'expected': '/'.join(sorted(expected)), 'bonds': 'one'}))
        elif largest > CF_TWO_BOND[1]:
            pass               # 60-150 Hz: reported above, belongs to no class
        elif largest >= CF_TWO_BOND[0]:
            two += 1
            expected = {_letter(n) for n in facts.cf_two} | {_letter(n) for n in facts.cf_one}
            if (facts.cf_two and order and order != 's' and not has_p
                    and not _fits(order, set(facts.cf_two) | set(facts.cf_one))):
                issues.append(Issue('c13_cf_multiplicity_mismatch', context={
                    'signal': signal.text, 'expected': '/'.join(sorted(expected - {''})), 'bonds': 'two'}))
        else:
            three += 1
    # Completeness: every carbon one or two bonds from F, and aromatic carbons three bonds away, is split.
    # Only 1J has a class of its own: a 2J can be as small as a long-range coupling (vinyl fluorides),
    # so below 150 Hz the signals are counted together.
    need_one, need_two, need_three = len(facts.cf_one), len(facts.cf_two), facts.cf_three_aromatic
    missing_one = max(0, need_one - one)
    missing_three = max(0, need_two + need_three - two - three - max(0, one - need_one))
    context = {'expected_one': need_one, 'expected_two': need_two, 'expected_three': need_three,
               'found_one': one, 'found_two': two, 'found_three': three}
    lines = _separate_lines(section, facts) if missing_one else None
    if lines:
        # The carbon bonded to fluorine is in the list, as the two lines of its doublet.
        # The pair found is evidence, not an assignment: a neighbouring signal (a C=O) can lie
        # between the two lines, so the message names the region and not the signals.
        issues.append(Issue('c13_cf_coupling_incomplete', 'separate_lines', {
            'low': f'{lines[0]:.0f}', 'high': f'{lines[1]:.0f}'}))
    elif missing_one or two + three < need_two or two < facts.cf_two_large:
        issues.append(Issue('c13_cf_coupling_incomplete', context=context))
    elif missing_three:
        # 1J and 2J are there; only couplings of a few hertz over three bonds are not shown.
        issues.append(Issue('c13_cf_long_range_coupling_missing', context=context))
    return issues
