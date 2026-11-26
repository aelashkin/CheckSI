"""Parse every coupling constant of every reported NMR signal."""

from __future__ import annotations

import re
from dataclasses import dataclass

_NUMBER = r'[-−–]?\d+(?:[.,]\d+)?'   # a 19F shift keeps its sign, whichever minus character is typed
_SIGNAL = re.compile(r'(?<![\d.])(' + _NUMBER + r')(?:\s*[-–—−]\s*(' + _NUMBER + r'))?\s*'
                     r'\(((?:[^()]|\([^()]*\))*)\)')
# One "J... = a, b Hz" group; the label keeps what stands between J and "=" (C-F, HF, H-P, 1, 2 ...).
# A constant, not an integral or atom count (2H, 1C, 3F) and not the order digit of the next constant (3J).
_VALUE = r'\d+(?:\.\d+)?(?![\d.])(?!\s*(?:H(?!z)|[A-GI-Za-zА-Яа-я]))'
_GROUP = re.compile(r'(\d?)\s*J\s*((?:[^=,;]|,\s?[A-Z](?![a-z]))?[^=;,]{0,14}?(?:,\s?[A-Z])?)\s*=\s*((?:' + _VALUE + r'\s*(?:Hz)?\s*(?:,|and|/)?\s*)+)')
_INTEGRAL = re.compile(r'(?<![\w.])(\d+(?:\.\d+)?)\s*[HН](?![a-zA-Z])')
_LETTERS = {'d': 1, 't': 2, 'q': 3, 'p': 4, 'h': 5}
_LONG = (('quint', 'p'), ('quin', 'p'), ('pent', 'p'), ('sext', 'h'), ('sept', 'S'), ('hept', 'S'))


@dataclass(frozen=True)
class Coupling:
    """One coupling constant with its written precision and nucleus label."""

    value: float
    decimals: int
    label: str = ''

    @property
    def heteronuclear(self) -> bool:
        """Labelled as a coupling to F or P (JH-F, JHP, JC-F ...)."""
        return bool(re.search(r'[FP]', self.label))


@dataclass(frozen=True)
class Signal:
    """A reported signal: shift or range, descriptor text and all its constants."""

    low: float
    high: float
    text: str
    multiplicity: str | None
    couplings: tuple[Coupling, ...]
    protons: float | None = None

    @property
    def first_order(self) -> str | None:
        """'dd', 'q', 's' ...; None for m, br or unrecognised descriptors."""
        code = self.multiplicity
        if not code:
            return None
        for word, letter in _LONG:
            code = code.replace(word, letter)
        return code if re.fullmatch(r's|[dtqphS]+', code) else None

    @property
    def unresolved(self) -> bool:
        """A multiplet or broad signal that may hide a coupling partner."""
        return self.first_order is None or (self.first_order != 's' and not self.couplings)


def _multiplicity(descriptor: str) -> str | None:
    head = descriptor.split(',')[0].strip().lower()
    head = re.sub(r'^(?:app(?:arent)?\.?|pseudo-?|ps\.?)\s*', '', head).replace(' ', '')
    return head or None


def _number(text: str) -> float:
    """Read a shift written with a decimal comma or a typographic minus."""
    return float(text.replace(',', '.').replace('−', '-').replace('–', '-'))


_FREQUENCY = re.compile(r'\((\d{2,3}(?:\.\d+)?)\s*MHz')
_BRACKET = re.compile(r'\((?:[^()]|\([^()]*\))*\)')


def spectrometer_frequency(section: str) -> float | None:
    """Observation frequency in MHz from the section header, e.g. "(101 MHz, CDCl3)"."""
    match = _FREQUENCY.search(section.split('δ', 1)[0] if 'δ' in section else section[:80])
    return float(match.group(1)) if match else None


def bare_shifts(section: str) -> list[float]:
    """Shifts listed without a descriptor: the plain numbers of a 13C list."""
    body = section.split('δ', 1)[1] if 'δ' in section else section
    kept, position = [], 0
    for match in _SIGNAL.finditer(body):
        kept.append(body[position:match.start()])
        position = match.end()
    kept.append(body[position:])
    plain = _BRACKET.sub(' ', ' '.join(kept))
    return [float(value) for value in re.findall(r'(?<![\w.])\d{1,3}(?:\.\d+)?(?![\w.])', plain)]


def parse_signals(section: str) -> list[Signal]:
    """Return every bracketed signal after the delta sign, with all constants."""
    body = section.split('δ', 1)[1] if 'δ' in section else section
    signals = []
    for match in _SIGNAL.finditer(body):
        first, second, descriptor = match.groups()
        values = [_number(first)] + ([_number(second)] if second else [])
        couplings = []
        for order, label, numbers in _GROUP.findall(descriptor):
            for number in re.findall(_VALUE, numbers):
                decimals = len(number.split('.')[1]) if '.' in number else 0
                couplings.append(Coupling(float(number), decimals, (order + label).strip()))
        integral = _INTEGRAL.search(descriptor)
        signals.append(Signal(min(values), max(values), match.group(0).strip(), _multiplicity(descriptor),
                              tuple(couplings), float(integral.group(1)) if integral else None))
    return signals
