"""Shared carbon evidence must remain bounded and consistent across consumers."""

import time
import unittest
from unittest.mock import patch

from checksi.analysis.nmr import _mask_coupling_nuclei, _parse_carbon_observations
from checksi.controller import CheckSIController
from tests.test_carbon_observations import carbon, config


class CarbonRoundThreeTests(unittest.TestCase):
    def test_unicode_couplings_preserve_the_independent_overlap(self):
        for label in ('¹J(13C,19F)', '²J(13C,19F)', '³J(13C,19F)', '⁴J(13C,19F)', '1J19F‐13C', '1J19F‑13C', '1J19F‒13C'):
            for overlap in ('', ', 2C'):
                with self.subTest(label=label, overlap=overlap):
                    observations = []
                    checked, tracker, _ = carbon('Cc1ccc(F)cc1', 'C7H7F',
                        f'161.0, 133.5, 130.5, 114.9 (d, {label} = 21.0 Hz{overlap}), 20.8',
                        observations_out=observations)
                    self.assertEqual(observations[3].represented_atoms, 2 if overlap else None)
                    self.assertTrue(any(line.startswith(f'C NMR Result: {6 if overlap else 5}.') for line in checked[0]))
                    # Only the genuine count mismatch is an analytical finding;
                    # the overlap refusal remains a hard DP5 preparation gate.
                    self.assertEqual(tracker.get_count('c13_count_structure_check'), int(bool(overlap)))
                    if overlap:
                        self.assertFalse(checked[6])

    def test_nested_descriptor_numbers_are_not_observed_peaks(self):
        for descriptor, count in (('d, J = 8.0 Hz, CH (Ar)', 5), ('d, J = 21.0 Hz (2C)', 6)):
            observations = []
            checked, _, _ = carbon('Cc1ccc(F)cc1', 'C7H7F',
                f'161.0, 133.5, 130.5, 114.9 ({descriptor}), 20.8', observations_out=observations)
            self.assertEqual([item.shift for item in observations], [161., 133.5, 130.5, 114.9, 20.8])
            self.assertTrue(any(line.startswith(f'C NMR Result: {count}.') for line in checked[0]))

    def test_later_delta_cannot_split_the_diagnostic_count_from_evidence(self):
        for shifts, expected in (('137.9, 129.1, 128.3, 125.3 (δ 125.2 minor), 21.5', 5),
                                 ('207.1, 30.9 (2C, δ 30.8 minor)', 3)):
            observations = []
            checked, _, _ = carbon('Cc1ccccc1' if expected == 5 else 'CC(=O)C',
                'C7H8' if expected == 5 else 'C3H6O', shifts, observations_out=observations)
            self.assertTrue(any(line.startswith(f'C NMR Result: {expected}.') for line in checked[0]))
            self.assertEqual(sum(item.represented_atoms or 1 for item in observations), expected)
            self.assertAlmostEqual(checked[4], expected / (7 if expected == 5 else 3))

    def test_nested_excess_after_later_delta_refuses_without_losing_the_run(self):
        for disabled in (False, True):
            cfg = config(disabled)
            result = CheckSIController(cfg).run_analysis([
                '1H NMR (400 MHz, CDCl3): δ 1.20 (s, 6H). 13C NMR (100 MHz, CDCl3): δ 60.0, 20.0.',
                '1H NMR (400 MHz, CDCl3): δ 1.20 (s, 6H). 13C NMR (100 MHz, CDCl3): δ 5.0, δ.1.0 ((a) 99999C).',
            ])
            self.assertFalse(result['aborted'])
            self.assertEqual(result['total_found'], 2)
            self.assertEqual(result['compounds'][1]['dp5_refusal_details'], {'cause': 'signal_limit_exceeded', 'limit': 10000})

    def test_budget_excludes_coupling_nuclei_but_keeps_explicit_counts(self):
        text = ', '.join(f'{index}.0 (d, ²J(13C,19F) = 21.0 Hz)' for index in range(800))
        observations, errors = _parse_carbon_observations(text)
        self.assertEqual(len(observations), 800)
        self.assertEqual(sum(item.represented_atoms or 1 for item in observations), 800)
        self.assertEqual(errors, [])

    def test_carbon_analysis_parses_observations_only_once(self):
        with patch('checksi.analysis.nmr._parse_carbon_observations', wraps=_parse_carbon_observations) as parse:
            carbon('CCO', 'C2H6O', '60.0, 20.0')
        self.assertEqual(parse.call_count, 1)

    def test_coupling_mask_and_nested_description_have_bounded_work(self):
        started = time.monotonic()
        self.assertEqual(_mask_coupling_nuclei('J' + ' ' * 20000 + 'x'), 'J' + ' ' * 20000 + 'x')
        observations, _ = _parse_carbon_observations('1.0 (' * 1500 + ')' * 1500)
        self.assertEqual(len(observations), 1)
        self.assertLess(time.monotonic() - started, 2.0)
