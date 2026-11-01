"""Real chemistry stays connected to the shared run engine and structure checks."""

import threading
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from rdkit import Chem

from checksi.analysis.cross_checks import compare_name_and_formula
from checksi.analysis.hrms_measurements import check_reported_masses
from checksi.application_runner import ApplicationRunner
from checksi.config_schema import AppConfig
from checksi.errors import ErrorCode
from checksi.events import ProgressEvent
from checksi.utils.error_handler import ErrorTracker


NMR = "1H NMR (400 MHz, CDCl3): δ 3.50 (s, 2H), 1.20 (s, 3H). 13C NMR (100 MHz, CDCl3): δ 60.0, 20.0. "


def engine_config(severity="warning"):
    """Construct an isolated configuration with external enrichment disabled."""
    return AppConfig.model_validate({
        "paths": {"input_path": "input.pdf", "output_dir": "."},
        "modes": {"deep_name_analysis": "off", "output_mode": "essential"},
        "solvents": {"entries": [{"name": "CDCl3", "aliases": [], "proton_shifts": [7.26], "carbon_shifts": [77.0]}]},
        "errors": {"default_severity": severity},
    })


class ChemistryEngineTests(unittest.TestCase):
    """Replace document/export boundaries, leaving all chemical calculations real."""

    def execute(self, cfg, paragraphs):
        events = []
        cfg.paths.output_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with (
            patch("checksi.document_parser.paragraphs_from_input", return_value=paragraphs),
            patch("checksi.controller._smiles_and_name_from_paragraph", return_value=(None, None, None, {}, [])),
            patch("checksi.application_runner.export_reports", return_value={
                "html_path": "findings.html", "pdf_path": "report.pdf", "error_stats_path": "errors.xlsx",
            }),
        ):
            result = ApplicationRunner(event_dispatcher=events.append).execute(cfg, "input.pdf", threading.Event())
        self.assertFalse(result.analysis_data["aborted"])
        self.assertTrue(any(isinstance(event, ProgressEvent) and event.message == "Analysis" for event in events))
        self.assertEqual(result.report_data["error_stats_path"], "errors.xlsx")
        return result.analysis_data

    def test_hrms_and_ea_findings_reach_shared_run_results(self):
        text = (NMR + "HRMS (ESI): calcd for C2H7O+ [M+H]+ 47.0491, found 47.0501. "
                "Anal. Calcd for C2H6O: C, 52.14; H, 13.13. Found: C, 51.73; H, 13.13.")
        result = self.execute(engine_config(), [text])
        self.assertEqual(result["total_found"], 1)
        self.assertEqual(result["error_stats"]["hrms_ppm_too_large"], 2)
        self.assertEqual(result["error_stats"]["ea_tolerance_exceeded"], 1)
        lines = result["compounds"][0]["all_warnings"]
        self.assertEqual(sum("hrms_ppm_too_large." in line for line in lines), 2)
        self.assertEqual(sum("ea_tolerance_exceeded." in line for line in lines), 1)

    def test_muted_invalid_formulas_do_not_abort_or_contaminate_next_compound(self):
        cfg = engine_config()
        cfg.errors.overrides = {
            ErrorCode.FORMULA_ATOM_SYMBOL_INVALID: "disabled",
            ErrorCode.FORMULA_INVALID_FORMAT: "disabled",
        }
        invalid = NMR + "HRMS (ESI): calcd for C2H6Xx+ [M+H]+ 47.0491, found 47.0491. Anal. Calcd for C2H6Xx: C, 52.14. Found: C, 52.14."
        valid = NMR + "HRMS (ESI): calcd for C2H7O+ [M+H]+ 47.0491, found 47.0491."
        result = self.execute(cfg, [invalid, "Next compound", valid])
        self.assertEqual(result["total_found"], 2)
        self.assertEqual(result["error_stats"]["formula_atom_symbol_invalid"], 0)
        self.assertEqual(result["error_stats"]["hrms_ppm_too_large"], 0)
        self.assertFalse(any("formula_atom_symbol_invalid." in line for compound in result["compounds"] for line in compound["all_warnings"]))
        empty = self.execute(cfg, [])
        self.assertEqual(empty["total_found"], 0)
        self.assertTrue(all(value == 0 for value in empty["error_stats"].values()))

    def test_structure_cross_check_preserves_fixed_oxygen_isotope(self):
        tracker = ErrorTracker()
        # 2*12 + 5*1.00782503223 + 36.965902602 + 17.99915961286.
        lines, formula, exchangeable = compare_name_and_formula(
            engine_config(), tracker, [], Chem.MolFromSmiles("[18OH]CCCl"),
            84.004187376, "C2H5[37Cl][18O]",
        )
        self.assertEqual(formula, "C2H5Cl[18O]")
        self.assertEqual(exchangeable, 1)
        self.assertEqual(lines, [])
        self.assertEqual(tracker.get_count("name_formula_mismatch"), 0)

    def test_reported_mass_outside_numeric_capacity_is_an_invalid_field(self):
        tracker = ErrorTracker()
        lines = check_reported_masses(
            engine_config(), tracker, "calcd " + "9" * 350 + ".0000, found 47.0491.",
            [], 47.0491, "C2H7O", "+", "+",
        )
        self.assertEqual(tracker.get_count("hrms_calcd_mass_missing"), 1)
        self.assertEqual(tracker.get_count("hrms_ppm_too_large"), 0)
        self.assertEqual(len(lines), 1)

    def test_prose_containing_instrument_letters_does_not_hide_hrms_fields(self):
        text = "Yellow resin. " + NMR + "HRMS (ESI): calcd for C2H7O+ [M+H]+ 47.0491, found 47.0501."
        result = self.execute(engine_config(), [text])
        self.assertEqual(result["error_stats"]["hrms_ppm_too_large"], 2)
        self.assertEqual(result["error_stats"]["hrms_calcd_mass_missing"], 0)
        self.assertEqual(result["error_stats"]["hrms_found_mass_missing"], 0)

    def test_oversized_adduct_does_not_abort_the_shared_run(self):
        text = NMR + "HRMS (ESI): calcd for C2H7O+ [M-H" + "9" * 350 + "]+ 47.0491, found 47.0491."
        result = self.execute(engine_config(), [text])
        self.assertEqual(result["total_found"], 1)
        self.assertEqual(result["error_stats"]["hrms_ppm_too_large"], 0)

    def test_semicolon_adjacent_nmr_does_not_become_ea_percentage_data(self):
        text = ("Anal. Calcd for C2H6O: C, 52.14; H, 13.13; O, 34.73. "
                "Found: C, 52.14; H, 13.13; O, 34.73; " + NMR)
        result = self.execute(engine_config(), [text])
        self.assertEqual(result["total_found"], 1)
        self.assertEqual(result["error_stats"]["ea_number_format_invalid"], 0)


if __name__ == "__main__":
    unittest.main()
