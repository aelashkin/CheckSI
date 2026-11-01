"""Exercise assessment accounting and visible reporting at the adapter boundary."""

from copy import deepcopy
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

from lxml import html
import fitz
import openpyxl
from rdkit import Chem

from checksi.dp5_enrichment import enrich_analysis_with_dp5
from checksi.dp5q.client import DP5qWorkerUnavailable
from checksi.dp5q.client import DP5qSession
from checksi.dp5q.contracts import CheckRequest, CheckResult
from checksi.dp5q.engine import check_compound
from checksi.dp5q.network import load_models
from checksi.events import LogEvent, ProgressEvent
from checksi.config_schema import DeepNameAnalysis, ErrorSeverity
from checksi.errors import ErrorCode
from checksi.file_io import write_dp5q_artifacts
from checksi.report_service import export_reports
from tests.test_dp5_review_fixes import review_config


def compound(index, smiles="CCO", shifts=None):
    """Build a complete already-parsed compound with report metadata."""
    return {"id": index, "smiles": smiles, "run_dp5": True,
            "dp5_c_signals_list": shifts or [18.2, 58.1], "dp5_carbon_groups": [],
            "original_text": f"Compound {index}", "all_warnings": [],
            "highlight_info": {"color": "blue"}}


class EnrichmentReportingTests(unittest.TestCase):
    """Infrastructure failures must stay distinct from scores and domain refusals."""

    def setUp(self):
        self.cfg = review_config()
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.cfg.paths.output_dir = Path(self.temp)
        self.events = []

    def enrich(self, compounds, effects):
        with patch("checksi.dp5_enrichment.DP5qSession") as session:
            session.return_value.__enter__.return_value.check.side_effect = effects
            data = enrich_analysis_with_dp5(
                self.cfg, {"total_found": len(compounds), "compounds": compounds},
                runtime_info={"model_id": "fixture"}, event_dispatcher=self.events.append)
        return data

    def export_text(self, data):
        before = deepcopy(data)
        result = export_reports(self.cfg, data, log_callback=lambda message: None)
        self.assertEqual(data, before)
        self.assertTrue(Path(result["error_stats_path"]).is_file())
        return '\n'.join(html.fromstring(Path(result['html_path']).read_text(encoding="utf-8")).itertext())

    def test_unavailable_worker_marks_every_eligible_unassessed_compound_in_docx(self):
        data = self.enrich([compound(i) for i in range(1, 4)], [
            DP5qWorkerUnavailable("worker_unavailable", "fixture startup failed")])
        self.assertEqual(data["dp5_summary"]["failed"], 3)
        self.assertEqual(data["dp5_summary"]["skipped"], 0)
        self.assertEqual(data["error_stats"]["dp5_analysis_failed"], 3)
        for item in data["compounds"]:
            self.assertEqual(item["dp5"]["status"], "failed")
            self.assertEqual(item["highlight_info"]["color"], "yellow")
            self.assertNotIn("score_raw", item["dp5"])
            self.assertIn("fixture startup failed", " ".join(item["all_warnings"]))
        text = self.export_text(data)
        self.assertIn("blue: 0, yellow: 3, red: 0", text)
        self.assertIn("n=0, skipped=0, failed=3", text)
        self.assertEqual(text.count("dp5_analysis_failed."), 3)
        self.assertEqual(sum("dp5 doctor" in event.message for event in self.events
                             if isinstance(event, LogEvent)), 1)

    def test_optional_artifact_failure_preserves_assessment_and_is_operational(self):
        self.cfg.dp5.keep_artifacts = True
        with patch("checksi.dp5_enrichment.write_dp5q_artifacts", side_effect=OSError("fixture collision")):
            data = self.enrich([compound(1)], [CheckResult(compound_id="1", status="assessed", score_raw=.5)])
        self.assertEqual(data["dp5_summary"]["n"], 1)
        self.assertEqual(data["dp5_summary"]["failed"], 0)
        self.assertEqual(data["error_stats"].get("dp5_analysis_failed", 0), 0)
        self.assertEqual(data["compounds"][0]["highlight_info"]["color"], "blue")
        text = self.export_text(data)
        self.assertIn("DP5q compatibility score: 50.00%", text)
        self.assertIn("optional artifacts could not be saved", text)
        self.assertNotIn("dp5_analysis_failed.", text)
        self.assertTrue(any("optional artifacts" in event.message for event in self.events
                            if isinstance(event, LogEvent)))

    def test_real_child_rejection_details_reach_activity_log_without_exporting_them(self):
        fixture = Path(__file__).parent / "fixtures/dp5q/controlled_worker.py"
        ready = {"model_id": "fixture", "score_version": "score", "preprocessing_id": "graph",
                 "geometry_policy": "single", "weights_sha256": "a" * 64}
        def create_session(*args, **kwargs):
            return DP5qSession(*args, **kwargs,
                              _command=[sys.executable, "-u", str(fixture), "request_rejected"])
        with patch("checksi.dp5q.client.check_availability", return_value=ready), \
             patch("checksi.dp5_enrichment.DP5qSession", side_effect=create_session):
            data = enrich_analysis_with_dp5(self.cfg, {"total_found": 1, "compounds": [compound(1)]},
                                           runtime_info=ready, event_dispatcher=self.events.append)
        detail = "controlled request schema rejection"
        self.assertTrue(any(detail in event.message for event in self.events if isinstance(event, LogEvent)))
        self.assertNotIn(detail, self.export_text(data))
        self.assertEqual(data["compounds"][0]["dp5"]["reason"], "protocol_error")

    def test_skips_explain_reason_in_progress_and_completed_statistics_use_assessed_only(self):
        self.cfg.modes.deep_name_analysis = DeepNameAnalysis.OFF
        items = [compound(1), compound(2), compound(3)]
        del items[2]["smiles"]
        data = self.enrich(items, [CheckResult(compound_id="1", status="assessed", score_raw=.2),
                                   CheckResult(compound_id="2", status="assessed", score_raw=.8)])
        summary = data["dp5_summary"]
        self.assertEqual((summary["min_score_percent"], summary["max_score_percent"],
                          summary["mean_score_percent"]), (20, 80, 50))
        logs = "\n".join(event.message for event in self.events if isinstance(event, LogEvent))
        progress = "\n".join(event.message for event in self.events if isinstance(event, ProgressEvent))
        self.assertIn("structure_unavailable_deep_name_analysis_off", logs + progress)
        self.assertIn("min=20.00%, max=80.00%, mean=50.00%", logs)

    def test_input_limits_have_short_domain_messages_without_pydantic_trace(self):
        cases = [("C" * 129, list(range(129)), "group_limit"),
                 ("C" * 513, list(range(513)), "atom_limit")]
        for smiles, shifts, reason in cases:
            with self.subTest(reason=reason):
                data = self.enrich([compound(1, smiles, shifts)], [])
                item = data["compounds"][0]
                self.assertEqual(item["dp5"]["reason"], reason)
                text = " ".join(item["all_warnings"])
                self.assertNotIn("pydantic", text.lower())
                self.assertNotIn("validation error", text.lower())
                self.assertIn("DP5q", text)

    def test_real_engine_results_export_boundary_colours_counts_tables_and_operational_warning(self):
        """Frozen software geometry isolates report behavior from embedding variation."""
        mol = next(iter(Chem.SDMolSupplier(str(Path(__file__).parent /
            "fixtures/dp5q/reference-network/toluene.sdf"), removeHs=False)))
        models = load_models()
        shifts = [138., 21., 129., 125., 128.]
        groups = [["C3", "C7"], ["C4", "C6"]]
        first = CheckRequest(compound_id="1", smiles="Cc1ccccc1", carbon_shifts=shifts,
                             carbon_groups=groups, include_geometry=True)
        second = CheckRequest(compound_id="2", smiles="Cc1ccccc1",
                              carbon_shifts=[value + 20 for value in shifts], carbon_groups=groups,
                              fixed_assignments={i: peak for i, peak in enumerate([2, 4, 1, 0, 3])},
                              include_geometry=True)
        high = check_compound(first, models, frozen_mol=mol)
        low = check_compound(second, models, frozen_mol=mol)
        self.assertEqual((high.status, low.status), ("assessed", "assessed"))
        self.assertLess(low.score_raw, high.score_raw)
        artifacts = write_dp5q_artifacts(Path(self.temp) / "saved-evidence", 1,
                                         second.model_dump(mode="json"), low.model_dump(mode="python"))
        saved_sdf = (artifacts / "candidate.sdf").read_text(encoding="utf-8")
        self.assertEqual(saved_sdf, low.diagnostics["geometry_sdf"])
        saved_mol = Chem.MolFromMolBlock(saved_sdf.split("$$$$")[0], removeHs=False)
        self.assertIsNotNone(saved_mol)
        self.assertEqual(saved_mol.GetNumAtoms(), mol.GetNumAtoms())
        self.cfg.dp5.score_threshold_percent = high.score_raw * 100
        self.cfg.dp5.keep_artifacts = True
        self.cfg.errors.overrides[ErrorCode.DP5_LOW_PROBABILITY] = ErrorSeverity.CRITICAL
        (Path(self.temp) / "dp5_artifacts").write_text("controlled file collision")
        items = [compound(i, "Cc1ccccc1", shifts) for i in range(1, 7)]
        for item in items:
            item["dp5_carbon_groups"] = groups
            item["highlight_info"]["cnmr_text"] = item["original_text"]
        data = self.enrich(items, [high, low,
            CheckResult(compound_id="3", status="failed", reason="calculation_failed",
                        diagnostics={"message": "Controlled numerical failure"}),
            CheckResult(compound_id="4", status="not_applicable", reason="unsupported_element",
                        diagnostics={"message": "Controlled applicability refusal"}),
            DP5qWorkerUnavailable("worker_unavailable", "controlled worker loss")])
        self.assertEqual([item["highlight_info"]["color"] for item in items],
                         ["blue", "red", "yellow", "blue", "yellow", "yellow"])
        self.assertEqual((data["dp5_summary"]["n"], data["dp5_summary"]["skipped"],
                          data["dp5_summary"]["failed"]), (2, 1, 3))
        score_values = [low.score_raw * 100, high.score_raw * 100]
        summary_numbers = (min(score_values), max(score_values), sum(score_values) / 2)
        self.assertEqual(tuple(data["dp5_summary"][name + "_score_percent"]
                               for name in ("min", "max", "mean")), summary_numbers)
        pdf_path = Path(self.temp) / "source.pdf"
        with fitz.open() as source:
            page = source.new_page()
            for i, item in enumerate(items):
                page.insert_text((60, 70 + i * 40), item["original_text"])
            source.save(pdf_path)
        before = deepcopy(data)
        paths = export_reports(self.cfg, data, pdf_path=str(pdf_path), log_callback=lambda message: None)
        self.assertEqual(data, before)
        document = html.fromstring(Path(paths["html_path"]).read_text(encoding="utf-8"))
        text = '\n'.join(document.itertext())
        self.assertIn("blue: 2, yellow: 3, red: 1", text)
        self.assertIn("n=2, skipped=1, failed=3", text)
        self.assertIn("min=%.2f%%, max=%.2f%%, mean=%.2f%%" % summary_numbers, text)
        self.assertEqual(text.count("dp5_low_probability."), 1)
        self.assertEqual(text.count("dp5_analysis_failed."), 3)
        compound_text = "\n".join(document.xpath('//div[@class="compound"]//text()'))
        self.assertEqual(compound_text.count("optional artifacts could not be saved"), 2)
        self.assertEqual(len(document.xpath('//table[@class="dp5"]')), 1)  # exact threshold is compatible
        rows = sorted(low.assignments, key=lambda row: -row["experimental_shift"])
        self.assertEqual([row[0].text_content() for row in document.xpath('//table[@class="dp5"]//tbody/tr')],
                         [row["atom_label"] for row in rows])
        self.assertEqual([row[3].text_content() for row in document.xpath('//table[@class="dp5"]//tbody/tr')],
                         ["" if row["scaled_error"] is None else f"{row['scaled_error']:.2f}" for row in rows])
        workbook = openpyxl.load_workbook(paths["error_stats_path"], read_only=True)
        counts = dict(workbook.active.iter_rows(min_row=2, values_only=True))
        self.assertEqual(counts["dp5_analysis_failed"], 3)
        self.assertEqual(counts["dp5_low_probability"], 1)
        self.assertEqual(counts["dp5_not_applicable"], 1)
        workbook.close()
        with fitz.open(paths["source_pdf_path"]) as output:
            annotations = [annotation for page in output for annotation in page.annots()]
            self.assertEqual(len(annotations), 7)
            contents = "\n".join(annotation.info["content"] for annotation in annotations)
            self.assertIn("n=2, skipped=1, failed=3", contents)
            self.assertIn("controlled worker loss", contents)
