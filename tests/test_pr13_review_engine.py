"""Reviewed manuscript forms through real extraction and the shared engine."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document

from checksi.application_runner import ApplicationRunner
from tests.test_chemistry_engine import engine_config, NMR


class ReviewEngineTests(unittest.TestCase):
    """Keep only document/export/name/conversion boundaries replaceable."""

    def run_paragraphs(self, text):
        with (
            tempfile.TemporaryDirectory() as output_dir,
            patch("checksi.document_parser.paragraphs_from_input", return_value=[text]),
            patch("checksi.application_runner.export_reports", return_value={}),
        ):
            cfg = engine_config()
            cfg.paths.output_dir = Path(output_dir)
            result = ApplicationRunner().execute(cfg, "input.pdf")
        self.assertFalse(result.analysis_data["aborted"])
        self.assertEqual(result.analysis_data["total_found"], 1)
        return result.analysis_data

    def test_dimer_ion_retains_charge_without_new_inverse_multiplier_policy(self):
        result = self.run_paragraphs(NMR + "HRMS (ESI) m/z: [2M+Na]+ Calcd for C42H56N4Na 639.4397; Found 639.4391.")
        self.assertEqual(result["error_stats"]["hrms_ion_charge_missing"], 0)
        self.assertEqual(result["error_stats"]["calcd_formula_mass_mismatch"], 0)

    def test_prose_esi_cannot_hide_later_owned_measurements(self):
        result = self.run_paragraphs("Colourless oil (see ESI). " + NMR + "HRMS (ESI) calcd for C2H7O+ [M+H]+ 47.0491, found 47.0501.")
        self.assertEqual(result["error_stats"]["hrms_ppm_too_large"], 2)
        self.assertEqual(result["error_stats"]["hrms_calcd_mass_missing"], 0)

    def test_wrapped_adduct_keeps_fluorine_driven_checks_connected(self):
        # C7H8FO+: 7*12 + 8*1.00782503223 + 18.99840316273
        # + 15.99491461957 - 0.00054858 = 127.05536946014.
        result = self.run_paragraphs(NMR + "HRMS (ESI) calcd for C7H8FO+ ([M+H]+) 127.0554, found 127.0554.")
        self.assertEqual(result["error_stats"]["required_nmr_missing"], 1)
        self.assertGreater(result["error_stats"]["c13_coupling_missing_with_f"], 0)

    def test_labelled_carbon_is_counted_by_the_nmr_consumer(self):
        nmr = NMR.replace("δ 60.0, 20.0", "δ 60.0, 50.0, 40.0, 30.0, 20.0, 10.0")
        # C5[13C]H13O2+ = 118.0949094932 - one electron.
        result = self.run_paragraphs(nmr + "HRMS (ESI) calcd for C5[13C]H13O2+ [M+H]+ 118.0944, found 118.0944.")
        self.assertEqual(result["error_stats"]["c13_count_exceeds_formula"], 0)
        self.assertEqual(result["error_stats"]["formula_not_found"], 0)

    def test_real_docx_cleanup_preserves_ppm_with_punctuation_and_mz_label(self):
        for suffix in ("47.0501;", "m/z 47.0501)."):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "input.docx"
                document = Document()
                document.add_paragraph("Compound (1)")
                document.add_paragraph(NMR + "HRMS (ESI) calcd for C2H7O+ [M+H]+ 47.0491, found " + suffix)
                document.save(path)
                with (
                    patch("checksi.application_runner.convert_docx_to_pdf", return_value=None),
                    patch("checksi.controller._smiles_and_name_from_paragraph", return_value=(None, None, None, {}, [])),
                    patch("checksi.application_runner.export_reports", return_value={}),
                ):
                    cfg = engine_config()
                    cfg.paths.output_dir = Path(directory) / "out"
                    result = ApplicationRunner().execute(cfg, str(path))
                self.assertEqual(result.analysis_data["total_found"], 1)
                self.assertEqual(result.analysis_data["error_stats"]["hrms_ppm_too_large"], 2)
                self.assertEqual(result.analysis_data["error_stats"]["hrms_found_mass_missing"], 0)
