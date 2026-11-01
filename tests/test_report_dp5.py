"""Real report exports preserve DP5 table policy and assignment ordering."""

from copy import deepcopy
import tempfile
import unittest
from pathlib import Path

from lxml import html

from checksi.config_schema import AppConfig
from checksi.report_service import export_reports, _format_dp5_summary


def table_values(table):
    return [[cell.text_content() for cell in row] for row in table.xpath('.//tr')]


class DP5ReportTests(unittest.TestCase):
    """Inspect saved DOCX content without changing the report renderer."""

    def export(self, directory, mode, probability, atoms, status="success", *, legacy=False, threshold=10.0):
        cfg = AppConfig.model_validate({
            "paths": {"input_path": "input.docx", "output_dir": directory},
            "modes": {"deep_name_analysis": "off", "output_mode": mode},
            "solvents": {"entries": []},
            "errors": {"default_severity": "warning", "overrides": {}},
            "dp5": {"enabled": True, "score_threshold_percent": threshold},
        })
        data = {"total_found": 1, "error_stats": {}, "compounds": [{
            "id": 1, "original_text": "Experimental description.", "all_warnings": [],
            "highlight_info": {"color": "blue"},
            "dp5": {"status": status, "probability_percent" if legacy else "score_percent": probability, "atoms": atoms},
        }]}
        before = deepcopy(data)
        metadata = export_reports(cfg, data, log_callback=lambda message: None)
        self.assertEqual(data, before)
        self.assertTrue(Path(metadata["html_path"]).is_file())
        self.assertTrue(Path(metadata["error_stats_path"]).is_file())
        return html.fromstring(Path(metadata["html_path"]).read_text(encoding="utf-8"))

    def test_threshold_and_mode_control_table_presence(self):
        atoms = [{"label": "C1", "assigned_shift": 60.0}]
        for mode, probability, expected_tables in [
            ("essential", 5.0, 1), ("essential", 10.0, 0), ("essential", 25.0, 0),
            ("detailed", 5.0, 1), ("detailed", 10.0, 1), ("detailed", 25.0, 1),
        ]:
            with self.subTest(mode=mode, probability=probability), tempfile.TemporaryDirectory() as tmp:
                document = self.export(tmp, mode, probability, atoms)
                self.assertEqual(len(document.xpath('//table[@class="dp5"]')), expected_tables)
                text = '\n'.join(document.itertext())
                self.assertIn(f"DP5q compatibility score: {probability:.2f}%", text)
                self.assertNotIn("probability", text.lower())

    def test_every_table_sorts_shifts_descending_with_none_last(self):
        atoms = [
            {"label": "C_none", "assigned_shift": None},
            {"label": "C_zero", "assigned_shift": 0.0},
            {"label": "C_low", "assigned_shift": -18.5},
            {"label": "C_high", "assigned_shift": 114.1, "calculated_shift": 112.0, "error": -2.1, "score_percent": 80.0, "atom_probability_percent": 7.0},
        ]
        for mode, probability in [("essential", 5.0), ("detailed", 25.0)]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                document = self.export(tmp, mode, probability, atoms)
                table = table_values(document.xpath('//table[@class="dp5"]')[0])
                self.assertEqual([row[0] for row in table[1:]], ["C_high", "C_zero", "C_low", "C_none"])
                self.assertEqual(table[1], ["C_high", "114.10", "112.00", "-2.10", "80.00%"])
                self.assertEqual(table[-1][1], "")
                self.assertEqual(table[0][4], "DP5q compatibility score")

    def test_legacy_result_aliases_render_with_the_same_score_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            document = self.export(tmp, "detailed", 8.0, [{
                "label": "C1", "assigned_shift": 60.0, "atom_probability_percent": 80.0,
            }], legacy=True)
            self.assertIn("DP5q compatibility score: 8.00%", list(document.itertext()))
            self.assertEqual(table_values(document.xpath('//table[@class="dp5"]')[0])[1][4], "80.00%")

    def test_summary_prefers_canonical_scores_and_accepts_legacy_aliases(self):
        legacy = {
            "enabled": True, "n": 2, "skipped": 1, "failed": 1,
            "min_probability_percent": 5, "max_probability_percent": 25,
            "mean_probability_percent": 15,
        }
        expected = "DP5q compatibility score statistics (n=2, skipped=1, failed=1): min=5.00%, max=25.00%, mean=15.00%."
        self.assertEqual(_format_dp5_summary(legacy), expected)
        canonical = {**legacy, "min_score_percent": 5, "max_score_percent": 25, "mean_score_percent": 15}
        canonical["mean_probability_percent"] = 99
        self.assertEqual(_format_dp5_summary(canonical), expected)

    def test_near_threshold_scores_never_display_as_the_threshold(self):
        atoms = [{"label": "C1", "assigned_shift": 60.0}]
        for probability, expected, flagged in [(9.99778, "9.998%", True), (10.004, "10.004%", False)]:
            with self.subTest(probability=probability), tempfile.TemporaryDirectory() as tmp:
                document = self.export(tmp, "essential", probability, atoms)
                self.assertIn(f"DP5q compatibility score: {expected}", list(document.itertext()))
                self.assertEqual(len(document.xpath('//table[@class="dp5"]')), int(flagged))

    def test_score_text_keeps_a_true_bound_at_extreme_precision(self):
        atoms = [{"label": "C1", "assigned_shift": 60.0}]
        for probability, threshold, expected in [(10.0000003, 10.0000004, "10.0000003%"), (0.0, 1e-8, "0%")]:
            with self.subTest(threshold=threshold), tempfile.TemporaryDirectory() as tmp:
                document = self.export(tmp, "essential", probability, atoms, threshold=threshold)
                self.assertIn(f"DP5q compatibility score: {expected}", list(document.itertext()))
                self.assertEqual(len(document.xpath('//table[@class="dp5"]')), 1)
        summary = {"enabled": True, "n": 1, "skipped": 0, "failed": 0,
                   "min_score_percent": 10.0000003, "max_score_percent": 10.0000003, "mean_score_percent": 10.0000003}
        self.assertTrue(_format_dp5_summary(summary, 10.0000004).endswith(
            "min=10.0000003%, max=10.0000003%, mean=10.0000003%."))

    def test_corrected_error_column_is_labelled_and_never_negative_zero(self):
        atoms = [{"label": "C1", "assigned_shift": 58.1, "calculated_shift": 60.28, "error": -2e-15, "score_percent": 55.0}]
        with tempfile.TemporaryDirectory() as tmp:
            table = table_values(self.export(tmp, 'detailed', 25.0, atoms).xpath('//table[@class="dp5"]')[0])
        self.assertEqual(table[0][3], "Corrected error")
        self.assertEqual(table[1][3], "0.00")

    def test_summary_statistics_agree_with_the_threshold(self):
        summary = {"enabled": True, "n": 1, "skipped": 0, "failed": 0,
                   "min_score_percent": 9.99778, "max_score_percent": 9.99778, "mean_score_percent": 9.99778}
        self.assertTrue(_format_dp5_summary(summary, 10.0).endswith("min=9.998%, max=9.998%, mean=9.998%."))
        self.assertTrue(_format_dp5_summary(summary).endswith("min=10.00%, max=10.00%, mean=10.00%."))

    def test_summary_lists_skip_reasons(self):
        summary = {"enabled": True, "n": 0, "skipped": 96, "failed": 0,
                   "skipped_reasons": {"structure_unavailable_deep_name_analysis_off": 96}}
        self.assertEqual(
            _format_dp5_summary(summary),
            "DP5q compatibility score statistics (n=0, skipped=96, failed=0; skip reasons: structure_unavailable_deep_name_analysis_off=96).",
        )

    def test_missing_atoms_have_placeholder_only_when_table_would_be_shown(self):
        for mode, probability, expected in [("essential", 5.0, True), ("essential", 25.0, False), ("detailed", 25.0, True)]:
            with self.subTest(mode=mode, probability=probability), tempfile.TemporaryDirectory() as tmp:
                document = self.export(tmp, mode, probability, [])
                self.assertEqual(len(document.xpath('//table[@class="dp5"]')), 0)
                self.assertEqual("DP5q carbon assignment table was not available." in list(document.itertext()), expected)

    def test_unsuccessful_dp5_results_have_no_probability_or_table(self):
        for status in ["skipped", "failed", "refused", "cancelled"]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                document = self.export(tmp, "detailed", 5.0, [{"label": "C1", "assigned_shift": 60.0}], status)
                self.assertEqual(len(document.xpath('//table[@class="dp5"]')), 0)
                self.assertFalse(any('DP5q compatibility score:' in text for text in document.itertext()))
