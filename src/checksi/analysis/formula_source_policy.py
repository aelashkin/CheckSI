"""Pure formula-source policy from Pavel's 2026-10-26 answers Q6–9.

Counts use molmass composition keys (``C``, ``13C``) and exact Fractions.
This module never resolves a chemical name or claims that a composition proves
an actual solvate identity. Callers retain source text and own diagnostics.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations
from math import gcd, lcm
import re
from typing import Literal

from molmass import ELEMENTS


Counts = Mapping[str, int | Fraction]
_MAX_LENGTH = 4096
_MAX_TOKENS = 256
_MAX_DEPTH = 8
_MAX_NUMBER_LENGTH = 18
_MAX_COUNT = 10**12
_COMPONENT_SEARCH_WORK_LIMIT = 4096
_NUMBER = re.compile(r'\d+(?:\.\d+)?')
_ATOM = re.compile(r'\[(\d+)([A-Z][a-z]?)\]|([A-Z][a-z]?)')
_FORCED_DISTINCT = frozenset(('2H', '13C', '15N', '17O', '18O'))

# Names follow the expert's list; formulas specify atom counts, not ion masses.
_SOLVENTS = {
    'H2O': 'H2O', 'MeOH': 'CH4O', 'EtOH': 'C2H6O', 'MeCN': 'C2H3N',
    'CH2Cl2': 'CH2Cl2', 'CHCl3': 'CHCl3', 'EtOAc': 'C4H8O2',
    'DMSO': 'C2H6OS', 'Et2O': 'C4H10O', 'acetone': 'C3H6O',
    'THF': 'C4H8O', 'toluene': 'C7H8',
}
_NEUTRAL_COMPONENTS = {'HCl': 'HCl', 'HBr': 'HBr', 'HI': 'HI', **_SOLVENTS, 'TFA': 'C2HF3O2'}
_ANIONS = {
    'Cl': 'Cl', 'Br': 'Br', 'I': 'I', 'BF4': 'BF4', 'PF6': 'PF6',
    'OTf': 'CF3O3S', 'NTf2': 'C2F6NO4S2', 'ClO4': 'ClO4',
    'SbF6': 'SbF6', 'BArF': 'C32H12BF24', 'OTs': 'C7H7O3S', 'OAc': 'C2H3O2',
}


@dataclass(frozen=True)
class ComponentAmount:
    """A named component and its exact stoichiometric amount."""

    name: str
    amount: Fraction

    @property
    def formula(self) -> str:
        return _NEUTRAL_COMPONENTS.get(self.name, _ANIONS.get(self.name, self.name))


@dataclass(frozen=True)
class ParentOption:
    """One possible integer parent; an option is not an identified physical parent."""

    parent_counts: dict[str, Fraction]
    components: tuple[ComponentAmount, ...]


@dataclass(frozen=True)
class EAFormulaPolicy:
    """Complete EA counts and all minimal fractional-solvent parent options.

    ``typo`` requires an exact certificate excluding an integer parent plus
    rational listed components. ``unsupported`` means this bounded parser did not establish a
    complete formula; it makes no author-error assertion. Dot components remain
    present in ``full_counts``; the first dot part is available only as a parent.
    """

    raw_formula: str
    status: Literal['accepted', 'typo', 'unsupported']
    full_counts: dict[str, Fraction]
    explicit_components: tuple[ComponentAmount, ...] = ()
    parent_options: tuple[ParentOption, ...] = ()
    reason: str = ''

    @property
    def comparable_parent(self) -> dict[str, Fraction] | None:
        """Return a parent only when all established options have identical counts."""
        if not self.parent_options:
            return None
        parent = self.parent_options[0].parent_counts
        return dict(parent) if all(option.parent_counts == parent for option in self.parent_options) else None


@dataclass(frozen=True)
class DifferenceExplanation:
    """Compositional explanations, with declared components preferred when valid.

    Absent a declaration, options use the fewest component kinds (at most two).
    Even one option is only consistent with the counts, not proof of identity.
    """

    options: tuple[tuple[ComponentAmount, ...], ...] = ()
    explicit: bool = False
    reason: str = ''

    @property
    def recognized(self) -> bool:
        return bool(self.options)


@dataclass(frozen=True)
class ComponentFeasibility:
    """An exact listed-component witness, an exhaustive refusal, or no proof."""

    status: Literal['feasible', 'infeasible', 'unknown']
    components: tuple[ComponentAmount, ...] = ()
    work: int = 0
    reason: str = ''


class _UnsupportedFormula(ValueError):
    pass


def _positive_count(value: str) -> Fraction:
    if len(value) > _MAX_NUMBER_LENGTH:
        raise _UnsupportedFormula('A numeric token exceeds the parser bound.')
    count = Fraction(value)
    if count <= 0 or count > _MAX_COUNT:
        raise _UnsupportedFormula('An atom count or coefficient is outside the supported range.')
    return count


def _merge(target: dict[str, Fraction], source: Counts, factor: Fraction = Fraction(1)) -> None:
    for key, count in source.items():
        target[key] = target.get(key, Fraction()) + factor * count
        if target[key] > _MAX_COUNT:
            raise _UnsupportedFormula('The total atom count exceeds the parser bound.')


def _parse_part(text: str) -> dict[str, Fraction]:
    """Parse a bounded formula without expanding atoms or group multipliers."""
    position = 0
    tokens = 0

    def count_after() -> Fraction:
        nonlocal position
        match = _NUMBER.match(text, position)
        if not match:
            return Fraction(1)
        position = match.end()
        return _positive_count(match.group())

    def group(depth: int) -> dict[str, Fraction]:
        nonlocal position, tokens
        if depth > _MAX_DEPTH:
            raise _UnsupportedFormula('Formula nesting exceeds the parser bound.')
        result = {}
        while position < len(text) and text[position] != ')':
            tokens += 1
            if tokens > _MAX_TOKENS:
                raise _UnsupportedFormula('Formula token count exceeds the parser bound.')
            if text[position] == '(':
                position += 1
                child = group(depth + 1)
                if position == len(text) or not child:
                    raise _UnsupportedFormula('An empty or unclosed group is unsupported.')
                position += 1
                _merge(result, child, count_after())
                continue
            match = _ATOM.match(text, position)
            if not match:
                raise _UnsupportedFormula('The complete formula contains unsupported notation.')
            isotope, symbol, plain_symbol = match.groups()
            symbol = symbol or plain_symbol
            if symbol == 'D' and isotope is None:
                symbol, isotope = 'H', '2'
            try:
                element = ELEMENTS[symbol]
                if isotope is not None and int(isotope) not in element.isotopes:
                    raise KeyError(isotope)
            except (KeyError, ValueError):
                raise _UnsupportedFormula('An element or fixed isotope is not in the locked mass table.') from None
            key = f'{int(isotope)}{symbol}' if isotope else symbol
            position = match.end()
            _merge(result, {key: count_after()})
        return result

    result = group(0)
    if position != len(text) or not result:
        raise _UnsupportedFormula('The complete formula was not parsed.')
    return result


def _component_counts() -> dict[str, dict[str, Fraction]]:
    return {name: _parse_part(formula) for name, formula in _NEUTRAL_COMPONENTS.items()}


def _integer(counts: Counts) -> bool:
    return all(Fraction(value).denominator == 1 for value in counts.values())


def _subtract(full: Counts, part: Counts) -> dict[str, Fraction] | None:
    difference = {key: Fraction(full.get(key, 0)) - part.get(key, 0) for key in full.keys() | part.keys()}
    if any(value < 0 for value in difference.values()):
        return None
    return {key: value for key, value in difference.items() if value}


def _scaled(counts: Counts, amount: Fraction) -> dict[str, Fraction]:
    return {key: Fraction(value) * amount for key, value in counts.items()}


def _fractional_parents(counts: Counts) -> tuple[ParentOption, ...]:
    options = []
    fractional_key = next(key for key, value in counts.items() if Fraction(value).denominator != 1)
    fractional_part = Fraction(counts[fractional_key]) % 1
    for name, solvent in _component_counts().items():
        if name not in _SOLVENTS or fractional_key not in solvent:
            continue
        # At most seven candidates for this curated solvent list. Only the
        # fractional equivalent is inferred; hidden whole equivalents are unknown.
        coefficient = int(solvent[fractional_key])
        for offset in range(coefficient):
            amount = (fractional_part + offset) / coefficient
            parent = _subtract(counts, _scaled(solvent, amount))
            if parent and _integer(parent):
                options.append(ParentOption(parent, (ComponentAmount(name, amount),)))
    return tuple(options)


def _unadjudicated_fractional_components(counts: Counts) -> bool:
    """Recognize bounded listed-component arithmetic without certifying a parent.

    Two fractional component amounts are solved exactly modulo integer atom
    counts. Partial acids and mixed solvents are pending; only a single listed
    fractional solvent is accepted by the adjudicated parent inference above.
    Amounts lie in [0, 1); hidden whole equivalents do not change integrality.
    """
    components = [(name, atoms) for name, atoms in _component_counts().items()
                  if all(counts.get(key, 0) > 0 for key in atoms)]
    for _, atoms in components:
        for key in atoms:
            residue = Fraction(counts[key]) % 1
            for offset in range(int(atoms[key])):
                amount = (residue + offset) / atoms[key]
                parent = _subtract(counts, _scaled(atoms, amount))
                if amount > 0 and parent and _integer(parent):
                    return True
    for (_, left), (_, right) in combinations(components, 2):
        for first, second in combinations(sorted(left.keys() | right.keys()), 2):
            a, b = left.get(first, 0), right.get(first, 0)
            c, d = left.get(second, 0), right.get(second, 0)
            determinant = a * d - b * c
            if not determinant:
                continue
            for first_offset in range(int(a + b) + 1):
                for second_offset in range(int(c + d) + 1):
                    first_count = Fraction(counts.get(first, 0)) % 1 + first_offset
                    second_count = Fraction(counts.get(second, 0)) % 1 + second_offset
                    x = (first_count * d - b * second_count) / determinant
                    y = (a * second_count - first_count * c) / determinant
                    if not (0 < x < 1 and 0 < y < 1):
                        continue
                    remainder = dict(counts)
                    remainder = _subtract(remainder, _scaled(left, x))
                    remainder = _subtract(remainder, _scaled(right, y)) if remainder else None
                    if remainder and _integer(remainder):
                        return True
            # Any independent pair determines both coefficients; other rows
            # only verify them through the complete remainder above.
            break
    return False


def _fractional_impossibility_invariant(counts: Counts) -> dict[str, int] | None:
    """Certify an impossible fractional fold with an integer left-nullspace row.

    For integer parent p and nonnegative rational listed amounts a, counts = p + A*a.
    Any integer vector v with v*A = 0 therefore has integral v*counts. A
    violated invariant proves impossibility; no certificate means unresolved,
    even if bounded single/two-component inference found no interpretation.
    """
    keys = tuple(sorted(counts))
    compatible = [atoms for atoms in _component_counts().values()
                  if all(counts.get(key, 0) > 0 for key in atoms)]
    rows = [[Fraction(atoms.get(key, 0)) for key in keys] for atoms in compatible]
    pivots = []
    for column in range(len(keys)):
        selected = next((index for index in range(len(pivots), len(rows)) if rows[index][column]), None)
        if selected is None:
            continue
        pivot_index = len(pivots)
        rows[pivot_index], rows[selected] = rows[selected], rows[pivot_index]
        factor = rows[pivot_index][column]
        rows[pivot_index] = [value / factor for value in rows[pivot_index]]
        for index, row in enumerate(rows):
            if index != pivot_index and row[column]:
                factor = row[column]
                rows[index] = [value - factor * pivot for value, pivot in zip(row, rows[pivot_index])]
        pivots.append(column)
    for free in (column for column in range(len(keys)) if column not in pivots):
        invariant = [Fraction() for _ in keys]
        invariant[free] = Fraction(1)
        for column, row in zip(pivots, rows):
            invariant[column] = -row[free]
        denominator = lcm(*(value.denominator for value in invariant))
        integers = [int(value * denominator) for value in invariant]
        divisor = gcd(*integers)
        integers = [value // divisor for value in integers]
        if sum(value * counts[key] for key, value in zip(keys, integers)).denominator != 1:
            return {key: value for key, value in zip(keys, integers) if value}
    return None


def parse_ea_formula(text: str) -> EAFormulaPolicy:
    """Read a complete EA formula and keep exact whole/fractional atom counts.

    Middle dots separate explicit components. ASCII dots are separators only
    when unambiguous with decimal counts; use a middle dot before coefficients
    after a numeric atom count. Leading isotope numbers require brackets, while
    a leading number on a dot component always means a multiplier.
    """
    if len(text) > _MAX_LENGTH:
        return EAFormulaPolicy(text, 'unsupported', {}, reason='Formula length exceeds the parser bound.')
    compact = re.sub(r'\s+', '', text).replace('•', '·').replace('ꞏ', '·')
    parts = re.split(r'·|(?<!\d)\.|\.(?!\d)', compact)
    if len(parts) > _MAX_TOKENS:
        return EAFormulaPolicy(text, 'unsupported', {}, reason='Too many explicit components.')
    full = {}
    explicit = []
    unsupported_component = False
    try:
        parent = _parse_part(parts[0])
        _merge(full, parent)
        components = {**_component_counts(), **{name: _parse_part(formula) for name, formula in _ANIONS.items()}}
        for part in parts[1:]:
            coefficient = _NUMBER.match(part)
            amount = _positive_count(coefficient.group()) if coefficient else Fraction(1)
            atom_text = part[coefficient.end():] if coefficient else part
            atoms = _parse_part(atom_text)
            _merge(full, atoms, amount)
            name = next((name for name, known in components.items() if known == atoms), None)
            if name is None or (amount.denominator != 1 and name not in _SOLVENTS):
                unsupported_component = True
            explicit.append(ComponentAmount(name or atom_text, amount))
    except (ValueError, OverflowError) as error:
        return EAFormulaPolicy(text, 'unsupported', {}, reason=str(error))
    if len(parts) == 1 and not _integer(full):
        symbols = re.findall(r'\[\d+[A-Z][a-z]?\]|[A-Z][a-z]?', parts[0])
        if len(symbols) != len(set(symbols)):
            return EAFormulaPolicy(text, 'unsupported', full, reason='Repeated-element fractional notation has no adjudicated reading.')
    if unsupported_component:
        return EAFormulaPolicy(text, 'unsupported', full, tuple(explicit), reason='An explicit component is outside the listed policy.')
    if explicit:
        if not _integer(parent):
            return EAFormulaPolicy(text, 'unsupported', full, tuple(explicit), reason='A fractional dot parent is unresolved.')
        options = (ParentOption(parent, tuple(explicit)),)
    elif _integer(full):
        options = (ParentOption(dict(full), ()),)
    else:
        options = _fractional_parents(full)
        if not options:
            if _unadjudicated_fractional_components(full):
                return EAFormulaPolicy(text, 'unsupported', full,
                    reason='Listed mixed or partial-acid components may explain the fractional counts; the parent policy is pending.')
            if _fractional_impossibility_invariant(full) is None:
                return EAFormulaPolicy(text, 'unsupported', full,
                    reason='The bounded parent inference did not exclude a higher-kind listed-component interpretation; review is pending.')
            return EAFormulaPolicy(text, 'typo', full, reason='Fractional counts violate an exact integer-parent/listed-component invariant.')
    return EAFormulaPolicy(text, 'accepted', full, tuple(explicit), options)


def _atom_mass(key: str) -> tuple[str, float]:
    match = re.fullmatch(r'(\d+)?([A-Z][a-z]?)', key)
    if match is None:
        raise ValueError(f'Unsupported composition key: {key!r}')
    isotope, symbol = match.groups()
    element = ELEMENTS[symbol]
    return symbol, element.isotopes[int(isotope)].mass if isotope else element.mass


def ea_molar_mass(counts: Counts) -> float:
    """Bulk EA molar mass; fixed-isotope atoms retain their fixed mass."""
    return sum(float(count) * _atom_mass(key)[1] for key, count in counts.items())


def ea_percentages(counts: Counts) -> dict[str, float]:
    """Mass percentages of the full formula, grouping fixed isotopes by element."""
    masses = {}
    for key, count in counts.items():
        symbol, mass = _atom_mass(key)
        masses[symbol] = masses.get(symbol, 0.0) + float(count) * mass
    total = sum(masses.values())
    if total <= 0:
        raise ValueError('EA mass percentages require a positive total mass.')
    return {symbol: mass / total * 100 for symbol, mass in masses.items()}


def ea_atom_count_percentages(counts: Counts) -> dict[str, float]:
    """Use ordinary element weights in numerators and actual isotope molar mass.

    This alternative groups D under H and 13C under C. Its percentages need
    not total 100: isotope masses remain in the common physical denominator.
    """
    total = ea_molar_mass(counts)
    if total <= 0:
        raise ValueError('EA mass percentages require a positive total mass.')
    ordinary_masses = {}
    for key, count in counts.items():
        symbol, _ = _atom_mass(key)
        ordinary_masses[symbol] = ordinary_masses.get(symbol, 0.0) + float(count) * ELEMENTS[symbol].mass
    return {symbol: mass / total * 100 for symbol, mass in ordinary_masses.items()}


def _single_amount(delta: Counts, atoms: Counts) -> Fraction | None:
    if delta.keys() != atoms.keys():
        return None
    amounts = {Fraction(delta[key]) / count for key, count in atoms.items()}
    return amounts.pop() if len(amounts) == 1 else None


def _two_amounts(delta: Counts, left: Counts, right: Counts) -> tuple[Fraction, Fraction] | None:
    keys = delta.keys() | left.keys() | right.keys()
    for first, second in combinations(sorted(keys), 2):
        a, b = left.get(first, 0), right.get(first, 0)
        c, d = left.get(second, 0), right.get(second, 0)
        determinant = a * d - b * c
        if not determinant:
            continue
        x = (delta.get(first, 0) * d - b * delta.get(second, 0)) / determinant
        y = (a * delta.get(second, 0) - delta.get(first, 0) * c) / determinant
        if x <= 0 or y <= 0 or x.denominator != 1 or y.denominator != 1:
            return None
        return (x, y) if all(left.get(key, 0) * x + right.get(key, 0) * y == delta.get(key, 0) for key in keys) else None
    return None


def explain_formula_difference(
    ea_counts: Counts,
    parent_counts: Counts,
    *,
    declared_components: tuple[ComponentAmount, ...] = (),
) -> DifferenceExplanation:
    """Explain EA minus HRMS parent using whole units of at most two kinds.

    A complete validated explicit declaration may include partial solvents and
    more than two kinds; it is preferred over inferred composition matches.
    Noneligible isotope labels remain distinct: callers may normalize reviewed
    natural labels first, but this helper never erases an unknown isotope.
    """
    delta = _subtract(ea_counts, parent_counts)
    if not delta:
        return DifferenceExplanation(reason='The formulas are identical or EA minus parent is not nonnegative.')
    components = _component_counts()
    if declared_components:
        declared = {}
        for component in declared_components:
            if (component.name not in components or component.amount <= 0
                    or (component.amount.denominator != 1 and component.name not in _SOLVENTS)):
                break
            _merge(declared, components[component.name], component.amount)
        else:
            if declared == delta:
                return DifferenceExplanation((declared_components,), True, 'The complete explicit components account for the difference.')
    single = []
    for name, atoms in components.items():
        amount = _single_amount(delta, atoms)
        if amount is not None and amount > 0 and amount.denominator == 1:
            single.append((ComponentAmount(name, amount),))
    if single:
        return DifferenceExplanation(tuple(single), reason='Composition is consistent with the listed whole-unit component options; identity is not established.')
    pairs = []
    for (left_name, left), (right_name, right) in combinations(components.items(), 2):
        amounts = _two_amounts(delta, left, right)
        if amounts is not None:
            pairs.append((ComponentAmount(left_name, amounts[0]), ComponentAmount(right_name, amounts[1])))
    return DifferenceExplanation(tuple(pairs), reason=('Composition is consistent with these two-kind options; identity is not established.' if pairs
                                                     else 'No whole-unit explanation using at most two listed component kinds was established.'))


def match_counterion(ea_counts: Counts, cation_counts: Counts, cation_charge: int) -> ComponentAmount | None:
    """Match one listed monovalent anion species with exact positive-charge balance.

    A +2 cation requires two copies of the same -1 anion. Mixed, unlisted or
    charge-unbalanced anions return None; callers must retain unknown status.
    """
    if isinstance(cation_charge, bool) or not isinstance(cation_charge, int) or cation_charge <= 0:
        return None
    delta = _subtract(ea_counts, cation_counts)
    if not delta:
        return None
    for name, formula in _ANIONS.items():
        if delta == _scaled(_parse_part(formula), Fraction(cation_charge)):
            return ComponentAmount(name, Fraction(cation_charge))
    return None


def counterion_disagreement(ea_counts: Counts, cation_counts: Counts, cation_charge: int | None) -> bool:
    """Identify impossible cation inventories or a complete wrong anion balance.

    An unlisted residual component keeps its unknown state. A missing cation
    atom or missing counter-ion contradicts a qualified positive HRMS parent.
    A complete listed-anion residual disagrees only when its total monovalent
    charge is wrong; balanced mixed-anion identity remains unadjudicated.
    """
    if not cation_charge or cation_charge < 0:
        return False
    delta = _subtract(ea_counts, cation_counts)
    if delta is None or not delta:
        return True
    anions = {name: _parse_part(formula) for name, formula in _ANIONS.items()}
    explained = False
    for atoms in anions.values():
        amount = _single_amount(delta, atoms)
        if amount is not None and amount.denominator == 1:
            if amount == cation_charge:
                return False
            explained = True
    for left, right in combinations(anions.values(), 2):
        amounts = _two_amounts(delta, left, right)
        if amounts is not None:
            if sum(amounts) == cation_charge:
                return False
            explained = True
    if explained:
        return True
    if delta.keys() <= {'Cl', 'Br', 'I'}:
        return sum(delta.values()) != cation_charge
    # A complete anion plus listed solvent is still an unadjudicated state,
    # even when all of the solvent's elements also occur in the cation.
    for atoms in anions.values():
        remainder = _subtract(delta, _scaled(atoms, Fraction(cation_charge)))
        if remainder and explain_formula_difference(remainder, {}).recognized:
            return False
    # An otherwise CH-only residual with a halide exposes a changed cation
    # inventory (for example CH2Cl); do not infer arbitrary polyatomic anions.
    return bool(delta.keys() <= {'C', 'H', 'Cl', 'Br', 'I'} and
                delta.keys() & {'Cl', 'Br', 'I'})


def has_three_kind_component_difference(ea_counts: Counts, parent_counts: Counts) -> bool:
    """Check exact whole three-kind explanations without expanding coefficients.

    Acceptance remains bounded to two kinds. A complete three-kind explanation
    establishes a pending relationship, never agreement or component identity.
    """
    delta = _subtract(ea_counts, parent_counts)
    if not delta or not _integer(delta):
        return False
    components = [atoms for atoms in _component_counts().values()
                  if all(value <= delta.get(key, 0) for key, value in atoms.items())]
    for first, second, third in combinations(components, 3):
        if _whole_three_amounts(delta, (first, second, third)):
            return True
    return False


def _whole_three_amounts(delta: Counts, components: tuple[Counts, Counts, Counts]) -> bool:
    """Solve three positive integer coefficients by exact RREF and congruences."""
    return _whole_three_coefficients(delta, components) is not None


def _whole_three_coefficients(
    delta: Counts, components: tuple[Counts, Counts, Counts],
) -> tuple[int, int, int] | None:
    """Return an exact positive integer witness for a curated component triple."""
    keys = sorted(delta.keys() | set().union(*(part.keys() for part in components)))
    rows = [[Fraction(part.get(key, 0)) for part in components] + [Fraction(delta.get(key, 0))]
            for key in keys]
    pivots = []
    for column in range(3):
        selected = next((index for index in range(len(pivots), len(rows)) if rows[index][column]), None)
        if selected is None:
            continue
        pivot_index = len(pivots)
        rows[pivot_index], rows[selected] = rows[selected], rows[pivot_index]
        factor = rows[pivot_index][column]
        rows[pivot_index] = [value / factor for value in rows[pivot_index]]
        for index, row in enumerate(rows):
            if index != pivot_index and row[column]:
                factor = row[column]
                rows[index] = [value - factor * pivot for value, pivot in zip(row, rows[pivot_index])]
        pivots.append(column)
    if any(not any(row[:3]) and row[3] for row in rows):
        return None
    if len(pivots) == 3:
        values = tuple(row[3] for row in rows[:3])
        return tuple(int(value) for value in values) if all(value >= 1 and value.denominator == 1 for value in values) else None
    # The curated list has no proportional component triples. Rank two leaves
    # one free integer: intersect positivity bounds with both integrality
    # congruences, then find its first feasible residue using generalized CRT.
    if len(pivots) != 2:
        return None
    free = next(column for column in range(3) if column not in pivots)
    lower, upper = 1, min(int(delta.get(key, 0) // count) for key, count in components[free].items())
    residue, modulus = 0, 1
    for row in rows[:2]:
        constant, slope = row[3], -row[free]
        if slope > 0:
            boundary = (1 - constant) / slope
            lower = max(lower, -(-boundary.numerator // boundary.denominator))
        elif slope < 0:
            boundary = (1 - constant) / slope
            upper = min(upper, boundary.numerator // boundary.denominator)
        elif constant < 1:
            return None
        denominator = lcm(constant.denominator, slope.denominator)
        a, b = int(slope * denominator), int(-constant * denominator)
        divisor = gcd(a, denominator)
        if b % divisor:
            return None
        next_modulus = denominator // divisor
        next_residue = ((b // divisor) * pow(a // divisor, -1, next_modulus)) % next_modulus if next_modulus > 1 else 0
        shared = gcd(modulus, next_modulus)
        if (next_residue - residue) % shared:
            return None
        step_modulus = next_modulus // shared
        step = (((next_residue - residue) // shared) * pow(modulus // shared, -1, step_modulus)) % step_modulus if step_modulus > 1 else 0
        residue += modulus * step
        modulus *= step_modulus
        residue %= modulus
    candidate = residue + max(0, -((residue - lower) // modulus)) * modulus
    if candidate > upper:
        return None
    values = [Fraction() for _ in range(3)]
    values[free] = Fraction(candidate)
    for column, row in zip(pivots, rows[:2]):
        values[column] = row[3] - row[free] * candidate
    return tuple(int(value) for value in values) if all(value >= 1 and value.denominator == 1 for value in values) else None


def listed_component_feasibility(
    ea_counts: Counts, parent_counts: Counts, *, work_limit: int | None = None,
) -> ComponentFeasibility:
    """Prove whole-unit feasibility across the complete neutral-component list.

    Unique-element components are forced first. Subproblems with up to three
    nonzero coefficients use exact equations; larger systems use memoized
    search with a shared work counter. Coefficient ranges never allocate or
    iterate beyond that budget. A partial search cannot establish a mismatch.
    """
    delta = _subtract(ea_counts, parent_counts)
    if delta is None or not _integer(delta):
        return ComponentFeasibility('infeasible', reason='nonnegative_integer_difference_required')
    if not delta:
        return ComponentFeasibility('feasible', reason='identical_inventories')
    named = tuple(_component_counts().items())
    keys = tuple(sorted(delta.keys() | set().union(*(atoms.keys() for _, atoms in named))))
    target = tuple(int(delta.get(key, 0)) for key in keys)
    vectors = tuple(tuple(int(atoms.get(key, 0)) for key in keys) for _, atoms in named)
    width = len(vectors)
    limit = _COMPONENT_SEARCH_WORK_LIMIT if work_limit is None else max(0, work_limit)
    work = 0
    memo = {}

    def spend():
        nonlocal work
        if work >= limit:
            return False
        work += 1
        return True

    def add(left, right):
        return tuple(a + b for a, b in zip(left, right))

    def search(remaining, active):
        if not spend():
            return 'unknown', None
        forced = [0] * width
        while any(remaining):
            active = tuple(index for index in active
                           if all(value <= available for value, available in zip(vectors[index], remaining)))
            owners = [(column, [index for index in active if vectors[index][column]])
                      for column, value in enumerate(remaining) if value]
            if any(not choices for _, choices in owners):
                return 'infeasible', None
            unique = next(((column, choices[0]) for column, choices in owners if len(choices) == 1), None)
            if unique is None:
                break
            column, index = unique
            coefficient, remainder = divmod(remaining[column], vectors[index][column])
            adjusted = tuple(value - coefficient * count for value, count in zip(remaining, vectors[index]))
            if remainder or any(value < 0 for value in adjusted):
                return 'infeasible', None
            forced[index] += coefficient
            remaining = adjusted
            active = tuple(item for item in active if item != index)
        if not any(remaining):
            return 'feasible', tuple(forced)
        state = remaining, active
        if state in memo:
            status, proof = memo[state]
            return status, add(proof, forced) if proof is not None else None

        def finish(status, proof=None):
            if status != 'unknown':
                memo[state] = status, proof
            return status, add(proof, forced) if proof is not None else None

        counts = {key: Fraction(value) for key, value in zip(keys, remaining) if value}
        for size in range(1, min(3, len(active)) + 1):
            for chosen in combinations(active, size):
                if not spend():
                    return finish('unknown')
                atoms = tuple(named[index][1] for index in chosen)
                if size == 1:
                    amount = _single_amount(counts, atoms[0])
                    amounts = (amount,) if amount is not None and amount > 0 and amount.denominator == 1 else None
                elif size == 2:
                    amounts = _two_amounts(counts, *atoms)
                else:
                    amounts = _whole_three_coefficients(counts, atoms)
                if amounts is not None:
                    proof = [0] * width
                    for index, amount in zip(chosen, amounts):
                        proof[index] = int(amount)
                    return finish('feasible', tuple(proof))
        if len(active) <= 3:
            return finish('infeasible')
        def maximum(index):
            return min(value // count for value, count in zip(remaining, vectors[index]) if count)
        selected = min(active, key=lambda index: (maximum(index), index))
        highest = maximum(selected)
        branches = min(highest + 1, limit - work)
        for offset in range(branches):
            if not spend():
                return finish('unknown')
            amount = highest - offset
            adjusted = tuple(value - amount * count for value, count in zip(remaining, vectors[selected]))
            status, proof = search(adjusted, tuple(index for index in active if index != selected))
            if status == 'feasible':
                proof = list(proof)
                proof[selected] += amount
                return finish(status, tuple(proof))
            if status == 'unknown':
                return finish(status)
        return finish('infeasible' if branches == highest + 1 else 'unknown')

    status, proof = search(target, tuple(range(width)))
    if status == 'feasible':
        rebuilt = tuple(sum(amount * vectors[index][column] for index, amount in enumerate(proof))
                        for column in range(len(keys)))
        if any(amount < 0 for amount in proof) or rebuilt != target:
            return ComponentFeasibility('unknown', work=work, reason='candidate_not_exact')
        components = tuple(ComponentAmount(name, Fraction(amount))
                           for (name, _), amount in zip(named, proof) if amount)
        return ComponentFeasibility('feasible', components, work, 'exact_integer_explanation')
    return ComponentFeasibility(status, work=work,
        reason='work_limit' if status == 'unknown' else 'exhaustive_integer_search')


def natural_isotope_eligibility() -> dict[str, frozenset[int]]:
    """Offline natural-peak labels: most abundant isotope and abundance >=10%.

    The locked molmass element table supplies abundances as fractions of one.
    The usual enrichment labels remain distinct even if a table changes.
    Unknown and below-threshold labels are deliberately absent, not rejected.
    """
    result = {}
    for element in ELEMENTS:
        isotopes = element.isotopes
        if not isotopes or not any(isotope.abundance > 0 for isotope in isotopes.values()):
            continue
        most_abundant = max(isotopes, key=lambda number: isotopes[number].abundance)
        result[element.symbol] = frozenset(
            number for number, isotope in isotopes.items()
            if (number == most_abundant or isotope.abundance >= 0.1)
            and f'{number}{element.symbol}' not in _FORCED_DISTINCT
        )
    # CIAAW 2024 gives 198Hg 10.04%, versus 9.97% in the locked table.
    # Eligibility uses that abundance update; isotope masses stay unchanged.
    result['Hg'] = result.get('Hg', frozenset()) | {198}
    return result


def integer_formula(counts: Counts) -> str | None:
    """Serialize an integer inventory without expanding atoms or losing labels."""
    if not counts or not _integer(counts):
        return None
    parts = []
    def hill_key(key):
        symbol = re.sub(r'^\d+', '', key)
        return (0 if symbol == 'C' else 1 if symbol == 'H' else 2, symbol, key)
    for key in sorted(counts, key=hill_key):
        value = counts[key]
        count = int(value)
        if count < 0:
            return None
        if count:
            symbol = f'[{key}]' if key[0].isdigit() else key
            parts.append(symbol + (str(count) if count != 1 else ''))
    return ''.join(parts) or None
