"""Pure text formatting shared by DP5q enrichment, reports and the GUI."""

from __future__ import annotations

from typing import Mapping

DEFAULT_DECIMALS = 2
MAX_DECIMALS = 6
# Seventeen significant digits tell any two different floats apart.
MAX_SIGNIFICANT_DIGITS = 17


def format_fixed(value: float, decimals: int = DEFAULT_DECIMALS) -> str:
    """Format a number with fixed decimals, never printing a negative zero."""
    text = f"{float(value):.{decimals}f}"
    if text.startswith("-") and float(text) == 0:
        return text[1:]
    return text


def format_score_and_threshold(score: float, threshold: float) -> tuple[str, str]:
    """Format a score and its threshold so the texts agree with the comparison.

    Two decimals are used unless they would show different values as equal,
    for example 9.9978 and 10.0 both as "10.00". Precision then grows up to six
    decimals; values that still look equal, such as 10.0000003 and 10.0000004,
    get as many significant digits as it takes to tell them apart. Both values
    are rounded the same way, so the texts keep the order of the values.
    """
    for decimals in range(DEFAULT_DECIMALS, MAX_DECIMALS + 1):
        score_text = format_fixed(score, decimals)
        threshold_text = format_fixed(threshold, decimals)
        if score_text != threshold_text or score == threshold:
            return score_text, threshold_text
    for digits in range(MAX_DECIMALS + 1, MAX_SIGNIFICANT_DIGITS + 1):
        score_text = _format_significant(score, digits)
        threshold_text = _format_significant(threshold, digits)
        if score_text != threshold_text:
            break
    return score_text, threshold_text


def _format_significant(value: float, digits: int) -> str:
    text = f"{float(value):.{digits}g}"
    if text.startswith("-") and float(text) == 0:
        return text[1:]
    return text


def format_skip_reasons(reasons: Mapping[str, int] | None) -> str:
    """List skip reasons with counts, most frequent first, e.g. ``a=3, b=1``."""
    items = [(str(reason), int(count)) for reason, count in (reasons or {}).items() if int(count) > 0]
    items.sort(key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{reason}={count}" for reason, count in items)
