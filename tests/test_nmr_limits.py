"""Bound document-controlled carbon expansion before materializing shifts."""

import tracemalloc
import unittest
from unittest.mock import patch

from checksi.analysis.nmr import (
    _CarbonSignalLimitExceeded, _parse_carbon_observations, calculate_nmr_number_count, check_nmr, preprocess_signals,
)
from checksi.config_schema import AppConfig
from checksi.controller import CheckSIController
from checksi.utils.error_handler import ErrorTracker


class CarbonSignalLimitTests(unittest.TestCase):
    def config(self, *, disabled=False, dp5=False):
        return AppConfig.model_validate({
            "paths": {"input_path": "input.pdf", "output_dir": "."},
            "modes": {"deep_name_analysis": "on" if dp5 else "off", "output_mode": "essential"},
            "solvents": {"entries": [{"name": "CDCl3", "aliases": [], "proton_shifts": [7.26], "carbon_shifts": [77.0]}]},
            "errors": {"default_severity": "warning", "overrides": {"nmr_signal_format": "disabled"} if disabled else {}},
            "dp5": {"enabled": dp5},
        })

    def carbon(self, cfg, signals, tracker, details):
        return check_nmr(
            cfg, tracker, f"13C NMR (100 MHz, CDCl3): δ {signals}.",
            "13C", 1, 2, 400, 0, 0, "C2H6", 0, 0,
            carbon_groups=[], dp5_refusal_details=details,
        )

    def test_general_nmr_limit_accepts_ten_thousand_represented_shifts(self):
        self.assertEqual(preprocess_signals("100.0 (10000C)"), [100.0] * 10000)
        self.assertEqual(len(preprocess_signals("100.0 (6000 C), 20.0 (4000С)")), 10000)

    def test_validated_coupling_annotations_share_the_cumulative_signal_budget(self):
        for label in ('J F,13C', 'J 13C,F', 'J H-13C'):
            for after in (False, True):
                def signals(first, second):
                    def fields(count):
                        return f'{label},{count}C' if after else f'{count}C,{label}'
                    return f'100.0 ({fields(first)}), 20.0 ({fields(second)})'
                with self.subTest(label=label, after=after):
                    observations, _ = _parse_carbon_observations(signals(6000, 4000))
                    self.assertEqual(sum(item.represented_atoms or 1 for item in observations), 10000)
                    with self.assertRaisesRegex(_CarbonSignalLimitExceeded, 'supported limit'):
                        calculate_nmr_number_count(self.config(), ErrorTracker(), signals(6000, 6000),
                                                   '13C', 1, 1, '', 0, 0)
                    for disabled in (False, True):
                        tracker, details = ErrorTracker(), {}
                        with patch('checksi.analysis.nmr.count_signals_by_regions') as regions:
                            checked = self.carbon(self.config(disabled=disabled), signals(6000, 6000), tracker, details)
                        regions.assert_not_called()
                        self.assertFalse(checked[6])
                        self.assertIsNone(checked[7])
                        self.assertEqual(details, {'cause': 'signal_limit_exceeded', 'limit': 10000})
                        self.assertFalse(any('C NMR Result:' in line for line in checked[0]))
                        self.assertEqual(tracker.get_count('nmr_signal_format'), int(not disabled))

    def test_long_shift_tokens_do_not_amplify_intermediate_text(self):
        signal = "0" * 1000 + "1.0 (1000C)"
        tracemalloc.start()
        try:
            actual = preprocess_signals(signal)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(actual, [1.0] * 1000)
        self.assertLess(peak, 512_000)

    def test_refuses_single_and_cumulative_excess_before_expansion(self):
        for signals in ["100.0 (10001C)", "100.0 (6000C), 20.0 (4001 C)"]:
            with self.subTest(signals=signals), self.assertRaisesRegex(_CarbonSignalLimitExceeded, "supported limit"):
                preprocess_signals(signals)

    def test_refuses_too_many_unannotated_shifts(self):
        with self.assertRaisesRegex(_CarbonSignalLimitExceeded, "supported limit"):
            preprocess_signals(", ".join(["100.0"] * 10001))

    def test_long_leading_zeros_preserve_bounded_numeric_counts(self):
        for digits, expected in [("0" * 5000 + "2", [100.0, 100.0]), ("0" * 5000, [])]:
            with self.subTest(length=len(digits)):
                self.assertEqual(preprocess_signals(f"100.0 ({digits}C)"), expected)
        tracker, details = ErrorTracker(), {}
        observations = []
        checked = check_nmr(self.config(), tracker, f"13C NMR (100 MHz, CDCl3): δ 100.0 ({'0' * 5000}2C).",
                            "13C", 1, 2, 400, 0, 0, "C2H6", 0, 0,
                            carbon_groups=[], dp5_refusal_details=details, observations_out=observations)
        self.assertEqual(checked[7], [100.0])
        self.assertEqual(observations[0].represented_atoms, 2)
        # The bounded annotation names two formal environments sharing one observation.
        self.assertTrue(checked[6])
        self.assertEqual(details, {})

    def test_refusal_stops_analysis_with_diagnostic_enabled_or_disabled(self):
        for disabled in [False, True]:
            for count in ["10001", "9" * 5000]:
                with self.subTest(disabled=disabled, count_length=len(count)):
                    tracker, details = ErrorTracker(), {}
                    checked = self.carbon(self.config(disabled=disabled), f"100.0 ({count}C)", tracker, details)
                    self.assertEqual(len(checked), 8)
                    self.assertFalse(checked[6])
                    self.assertIsNone(checked[7])
                    self.assertEqual(checked[5], [])
                    self.assertEqual(details, {"cause": "signal_limit_exceeded", "limit": 10000})
                    self.assertFalse(any("C NMR Result:" in line for line in checked[0]))
                    self.assertFalse(any("c13_count_structure_check." in line for line in checked[0]))
                    self.assertEqual(tracker.get_count("nmr_signal_format"), 0 if disabled else 1)

    def test_count_normalization_preserves_decimal_shift_annotations(self):
        rows = (
            ("170.05 C=O", 170.05), ("170.005C=O", 170.005),
            ("100.0 (2.5C)", 100.0), ("100.0 (2.5 C)", 100.0), ("100.0 (2.5С)", 100.0),
        )
        for text, expected in rows:
            with self.subTest(text=text):
                self.assertEqual(preprocess_signals(text + ", 20.0 (0002C)"), [expected, 20.0, 20.0])
                checked = self.carbon(self.config(), text, ErrorTracker(), {})
                self.assertEqual(checked[7], [expected])
                self.assertTrue(any(line.lstrip("❗ ").startswith("C NMR Result: 1.") for line in checked[0]))

    def test_nested_and_unbalanced_counts_use_the_structured_limit_refusal(self):
        for ending in ("C)", "C))"):
            signals = f"(5C 100.0 ({'9' * 5000}{ending}"
            with self.subTest(ending=ending), self.assertRaisesRegex(_CarbonSignalLimitExceeded, "supported limit"):
                preprocess_signals(signals)
            for disabled in (False, True):
                with self.subTest(ending=ending, disabled=disabled):
                    tracker, details = ErrorTracker(), {}
                    checked = self.carbon(self.config(disabled=disabled), signals, tracker, details)
                    self.assertFalse(checked[6])
                    self.assertIsNone(checked[7])
                    self.assertEqual(details, {"cause": "signal_limit_exceeded", "limit": 10000})
                    self.assertEqual(sum("supported limit of 10000" in warning for warning in checked[0]), int(not disabled))
                    self.assertEqual(tracker.get_count("nmr_signal_format") == 0, disabled)

    def test_counter_and_preparation_share_the_limit_for_all_counted_forms(self):
        cases = [
            "100.0 (12345-1.0-2.0C)",
            "100.0 (12345 -1.0-2.0C)",
            "(9000C), (9000C), (9000C)",
            "100.0 " + ", ".join(["(9000C)"] * 3),
            f"100.0 (12345-1.0-2.{'9' * 5000}C)",
        ]
        for signals in cases:
            with self.subTest(signals_length=len(signals)), self.assertRaisesRegex(_CarbonSignalLimitExceeded, "supported limit"):
                preprocess_signals(signals)
            with self.subTest(counter_length=len(signals)), self.assertRaisesRegex(_CarbonSignalLimitExceeded, "supported limit"):
                calculate_nmr_number_count(self.config(), ErrorTracker(), signals, "13C", 1, 1, "", 0, 0)
            for disabled in (False, True):
                with self.subTest(signals_length=len(signals), disabled=disabled):
                    tracker, details = ErrorTracker(), {}
                    checked = self.carbon(self.config(disabled=disabled), signals, tracker, details)
                    self.assertFalse(checked[6])
                    self.assertIsNone(checked[7])
                    self.assertEqual(details, {"cause": "signal_limit_exceeded", "limit": 10000})
                    self.assertEqual(sum("supported limit of 10000" in warning for warning in checked[0]), int(not disabled))
                    self.assertEqual(tracker.get_count("nmr_signal_format") == 0, disabled)

    def test_counts_exposed_after_a_later_delta_use_the_structured_limit_refusal(self):
        # The ordinary counter starts at the next δ, where a nested count is top-level.
        for counts in ("(10001C)", "(6000C), (6000C)", f"({'9' * 5000}C)"):
            signals = f"60.0, 20.0 (2C, δ 1.0, {counts})"
            description = ("1H NMR (400 MHz, CDCl3): δ 1.20 (s, 6H). "
                           f"13C NMR (100 MHz, CDCl3): δ {signals}.")
            for disabled in (False, True):
                with self.subTest(counts_length=len(counts), disabled=disabled):
                    tracker, details = ErrorTracker(), {}
                    checked = self.carbon(self.config(disabled=disabled), signals, tracker, details)
                    self.assertFalse(checked[6])
                    self.assertIsNone(checked[7])
                    self.assertEqual(details, {"cause": "signal_limit_exceeded", "limit": 10000})
                    self.assertEqual(sum("supported limit of 10000" in warning for warning in checked[0]), int(not disabled))
                    self.assertFalse(any("C NMR Result:" in line for line in checked[0]))

                    result = CheckSIController(self.config(disabled=disabled)).run_analysis([description])
                    self.assertFalse(result["aborted"])
                    self.assertEqual(result["total_found"], 1)
                    compound = result["compounds"][0]
                    self.assertFalse(compound["run_dp5"])
                    self.assertIsNone(compound["dp5_c_signals_list"])
                    self.assertEqual(compound["dp5_refusal_details"], {"cause": "signal_limit_exceeded", "limit": 10000})
                    self.assertEqual(sum("supported limit of 10000" in warning for warning in compound["all_warnings"]), int(not disabled))

    def test_refused_counts_retain_the_controller_result(self):
        for signals in (f"100.0 (1C 200.0 ({'9' * 5000}C))", "(9000C), (9000C), (9000C)"):
            description = ("1H NMR (400 MHz, CDCl3): δ 1.20 (s, 6H). "
                           f"13C NMR (100 MHz, CDCl3): δ {signals}.")
            for disabled in (False, True):
                with self.subTest(signals_length=len(signals), disabled=disabled):
                    result = CheckSIController(self.config(disabled=disabled)).run_analysis([description])
                    self.assertFalse(result["aborted"])
                    self.assertEqual(result["total_found"], 1)
                    compound = result["compounds"][0]
                    self.assertFalse(compound["run_dp5"])
                    self.assertIsNone(compound["dp5_c_signals_list"])
                    self.assertEqual(compound["dp5_refusal_details"], {"cause": "signal_limit_exceeded", "limit": 10000})
                    self.assertEqual(sum("supported limit of 10000" in warning for warning in compound["all_warnings"]), int(not disabled))
                    self.assertEqual(result["error_stats"]["nmr_signal_format"] == 0, disabled)

    def test_disabled_diagnostic_cannot_send_refused_spectrum_to_dp5(self):
        from checksi.dp5_enrichment import enrich_analysis_with_dp5

        cfg, tracker, details = self.config(disabled=True, dp5=True), ErrorTracker(), {}
        checked = self.carbon(cfg, "100.0 (10001C)", tracker, details)
        compound = {
            "id": 1, "smiles": "CC", "all_warnings": checked[0],
            "dp5_carbon_groups": checked[5], "run_dp5": checked[6],
            "dp5_c_signals_list": checked[7], "dp5_refusal_details": details,
        }
        with patch("checksi.dp5_enrichment.check_availability", return_value={"model_id": "fixture"}), patch("checksi.dp5_enrichment.DP5qSession") as session:
            result = enrich_analysis_with_dp5(cfg, {"compounds": [compound], "error_stats": {}})
        session.return_value.__enter__.return_value.check.assert_not_called()
        self.assertEqual(result["dp5_summary"]["successful"], 0)
        self.assertEqual(compound["dp5"]["reason"], "carbon_signal_limit")
        self.assertEqual(result["error_stats"].get("dp5_not_applicable", 0), 0)
        self.assertEqual(compound["all_warnings"], checked[0])

    def test_enabled_limit_diagnostic_is_not_repeated_by_dp5(self):
        from checksi.dp5_enrichment import enrich_analysis_with_dp5

        tracker, details = ErrorTracker(), {}
        checked = self.carbon(self.config(dp5=True), "100.0 (10001C)", tracker, details)
        warnings = list(checked[0])
        compound = {
            "id": 1, "smiles": "CC", "all_warnings": warnings,
            "dp5_carbon_groups": checked[5], "run_dp5": checked[6],
            "dp5_c_signals_list": checked[7], "dp5_refusal_details": details,
        }
        with patch("checksi.dp5_enrichment.DP5qSession") as session:
            result = enrich_analysis_with_dp5(
                self.config(dp5=True), {"compounds": [compound], "error_stats": tracker.to_dict()},
                runtime_info={"model_id": "fixture"},
            )
        session.return_value.__enter__.return_value.check.assert_not_called()
        self.assertEqual(compound["dp5"], {"status": "skipped", "reason": "carbon_signal_limit"})
        self.assertEqual(compound["all_warnings"], checked[0])
        self.assertEqual(result["error_stats"]["nmr_signal_format"], 1)
        self.assertEqual(result["error_stats"].get("dp5_not_applicable", 0), 0)


if __name__ == "__main__":
    unittest.main()
