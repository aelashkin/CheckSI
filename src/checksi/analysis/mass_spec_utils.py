# Standard library imports
import math
import re
from itertools import combinations

# Third-party imports
from molmass import ELEMENTS

# The mass of an electron, used for adjusting mass based on ion charge.
ELECTRON_MASS = 0.00054858

# Match Formula's most-abundant-isotope convention; table iteration may start
# with a less abundant isotope (for example, boron-10 rather than boron-11).
ELEMENT_MASSES = {
    elem: max(ELEMENTS[elem].isotopes.values(), key=lambda isotope: isotope.abundance).mass
    for elem in ['C', 'N', 'O', 'S', 'Na', 'K', 'B', 'Si', 'H', 'F', 'I', 'P']
}
# Using 'Cl-35' format for consistency with desired output format.
ELEMENT_MASSES.update({
    'Cl-35': 34.96885, 'Cl-37': 36.96590,
    'Br-79': 78.91834, 'Br-81': 80.91629,
    'electron': ELECTRON_MASS,
})


def find_elemental_combinations_for_mass(target_mass, max_count=7, tolerance=0.0002):
    """
    Finds combinations of elements that sum to a target mass, to explain a discrepancy.
    Searches for combinations of 1, 2, or 3 unique elements.

    Args:
        target_mass: The mass difference to explain.
        max_count: The maximum number of atoms for any single element in a combination.
        tolerance: The allowable difference between the combination's mass and the target mass.

    Returns:
        A list of possible combinations, where each combination is a list of (element, count) tuples.
    """
    # No three-element combination within the configured atom limits can reach
    # a larger magnitude. Stop before divisions can overflow on malformed input.
    upper_bound = max_count * sum(sorted(ELEMENT_MASSES.values())[-3:])
    if not math.isfinite(target_mass) or abs(target_mass) > upper_bound + tolerance:
        return []
    results = []
    element_items = list(ELEMENT_MASSES.items())
    limited_elements = {'Na', 'K', 'electron'} # These elements are unlikely to appear in high counts

    # 1. Check single element contributions
    for element, mass in element_items:
        count = round(target_mass / mass)
        if count == 0:
            continue
        if element in limited_elements and abs(count) > 2:
            continue
        
        if -max_count <= count <= max_count and abs(mass * count - target_mass) <= tolerance:
            results.append([(element, count)])

    # 2. Check two-element combinations (brute-force)
    for (elem1, mass1), (elem2, mass2) in combinations(element_items, 2):
        for c1 in range(-max_count, max_count + 1):
            if c1 == 0:
                continue
            if elem1 in limited_elements and abs(c1) > 2:
                continue
            
            for c2 in range(-max_count, max_count + 1):
                if c2 == 0:
                    continue
                if elem2 in limited_elements and abs(c2) > 2:
                    continue

                total_mass = mass1 * c1 + mass2 * c2
                if abs(total_mass - target_mass) <= tolerance:
                    results.append(sorted([(elem1, c1), (elem2, c2)]))

    # 3. Check three-element combinations (heuristic)
    for (elem1, mass1), (elem2, mass2), (elem3, mass3) in combinations(element_items, 3):
        for c1 in range(-max_count, max_count + 1):
            if c1 == 0:
                continue
            if elem1 in limited_elements and abs(c1) > 2:
                continue
            
            for c2 in range(-max_count, max_count + 1):
                if c2 == 0:
                    continue
                if elem2 in limited_elements and abs(c2) > 2:
                    continue

                # Heuristically calculate the count for the third element
                remaining_mass = target_mass - (mass1 * c1) - (mass2 * c2)
                c3 = round(remaining_mass / mass3)

                if c3 == 0:
                    continue
                if elem3 in limited_elements and abs(c3) > 2:
                    continue

                if -max_count <= c3 <= max_count:
                    total_mass = mass1 * c1 + mass2 * c2 + mass3 * c3
                    if abs(total_mass - target_mass) <= tolerance:
                        results.append(sorted([(elem1, c1), (elem2, c2), (elem3, c3)]))


    # Remove duplicate combinations
    unique_results = []
    seen = set()
    # The `sorted()` wrapper is removed from `results` to preserve the order in which they were found.
    for res in results:
        res_tuple = tuple(res)
        if res_tuple not in seen:
            unique_results.append(res)
            seen.add(res_tuple)
    
    return unique_results


def format_combination(combination):
    """
    Formats a combination of elements and counts into a human-readable string.
    Example: [('C', 1), ('H', -2)] -> "C-H2".

    Args:
        combination: A list of (element, count) tuples.

    Returns:
        A formatted string representing the elemental combination.
    """
    parts = []
    for element, count in combination:
        # Format isotope names like 'Cl-35' to '[Cl-35]' for display.
        display_element = re.sub(r'([A-Za-z]+-\d+)', r'[\1]', element)

        if count == 1:
            parts.append(f"+{display_element}")
        elif count == -1:
            parts.append(f"-{display_element}")
        elif count > 1:
            parts.append(f"+{display_element}{count}")
        elif count < -1:
            parts.append(f"-{display_element}{abs(count)}")
    
    # Join parts and clean up the leading "+"
    result = "".join(parts)
    if result.startswith('+'):
        return result[1:]
    return result
