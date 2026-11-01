"""Isotope notation and variant generation for formula cross-checks."""

import itertools
import re
from collections.abc import Iterable
from fractions import Fraction

from molmass import Formula, FormulaError
from checksi.analysis.formula_source_policy import natural_isotope_eligibility

# A dictionary of elements for which isotopic variants are considered in mass calculations.
ISOTOPIC_ELEMENTS = {
    "Cl": ["[35Cl]", "[37Cl]"],
    "Br": ["[79Br]", "[81Br]"],
    "N":  ["[14N]", "[15N]"],
    "O":  ["[16O]", "[18O]"],
}


_NATURAL_ISOTOPE_ELEMENTS = {
    f'{number}{element}': element
    for element, numbers in natural_isotope_eligibility().items() for number in numbers
}


def _comparison_counts(counts: Iterable[tuple[str, int | Fraction]]) -> dict[str, int | Fraction]:
    """Collapse reviewed natural-isotope peak labels, retaining enrichment."""
    normalized = {}
    for key, count in counts:
        element = _NATURAL_ISOTOPE_ELEMENTS.get(key, key)
        normalized[element] = normalized.get(element, 0) + count
    return normalized


def _comparison_formula(formula: str) -> str:
    """Normalize only comparison spelling; callers retain original source masses."""
    return re.sub(
        r'\[(\d+[A-Z][a-z]?)\]',
        lambda match: _NATURAL_ISOTOPE_ELEMENTS.get(match.group(1), match.group()),
        formula,
    )


def _count_isotopic_elements(formula_string: str) -> dict[str, int]:
    """Count unspecified isotopes by parsed symbol, including group multipliers."""
    try:
        composition = Formula(formula_string).composition()
    except (FormulaError, ValueError, OverflowError):
        return {}
    return {
        element: composition[element].count
        for element in ISOTOPIC_ELEMENTS
        if element in composition
    }


def _generate_isotopic_formula_combinations(base_formula: str, atom_counts: dict[str, int]) -> list[str]:
    """Generates all possible formula strings based on isotopic variants."""
    isotopic_formulas = []

    # Create all combinations for each element type
    combos_by_element = {
        element: list(itertools.combinations_with_replacement(ISOTOPIC_ELEMENTS[element], count))
        for element, count in atom_counts.items()
    }

    # Expand grouped counts before substituting individual element tokens.
    if any(bracket in base_formula for bracket in "(){}.") or re.search(r'\[(?!\d+[A-Z][a-z]?\])', base_formula):
        base_formula = Formula(base_formula).formula
    element_keys = list(combos_by_element.keys())
    for combo_product in itertools.product(*(combos_by_element[key] for key in element_keys)):
        replacements = dict(zip(element_keys, map(''.join, combo_product)))

        def replace_token(match: re.Match) -> str:
            # Explicit isotope tokens must remain fixed. Repeated plain symbols
            # are collected at their first occurrence to include mixed variants.
            element = match.group(1)
            if element not in atom_counts:
                return match.group(0)
            return replacements.pop(element, '')

        new_formula = re.sub(r'\[\d+[A-Z][a-z]?\]\d*|([A-Z][a-z]?)\d*', replace_token, base_formula)
        isotopic_formulas.append(new_formula)

    return isotopic_formulas


def get_isotopic_masses(formula_string: str) -> list[tuple[str, float]]:
    """
    Calculates all possible monoisotopic masses for a formula given its isotopic variants.

    Args:
        formula_string: The chemical formula.

    Returns:
        A list of tuples, where each tuple is (isotopic_formula_string, mass).
    """
    atom_counts = _count_isotopic_elements(formula_string)
    if not atom_counts:
        return []

    isotopic_formulas = _generate_isotopic_formula_combinations(formula_string, atom_counts)

    masses = []
    for iso_formula in isotopic_formulas:
        try:
            mol_mass = Formula(iso_formula).monoisotopic_mass
            masses.append((iso_formula, mol_mass))
        except FormulaError:
            # Silently ignore if a generated formula is invalid, though this shouldn't happen.
            continue

    return masses


def replace_common_isotope_formats(formula_string: str) -> str:
    """
    Replaces common non-standard isotope notations with the standard `[isotope]element` format.
    Example: "35Cl" -> "[35Cl]".

    Args:
        formula_string: The formula string to process.

    Returns:
        The processed formula string.
    """
    if '2H' in formula_string and 'H' in formula_string.split('2H')[0]:
        formula_string = re.sub(r'(?<!\[)2H', '[2H]', formula_string)
    isotopes_to_format = ['35Cl', '37Cl', '79Br', '81Br', '15N', '13C', '18O']
    for isotope in isotopes_to_format:
        # Use a negative lookbehind to avoid replacing already formatted isotopes like in C[13C]
        pattern = rf'(?<=[\d\+\-\]a-zA-GI-Z]){isotope}(?![a-z])'   # Isotopes are replaced only if there is no 'H' before the isotope, but digits, +, -, or ] may be present
        formula_string = re.sub(pattern, f'[{isotope}]', formula_string)
    return formula_string
