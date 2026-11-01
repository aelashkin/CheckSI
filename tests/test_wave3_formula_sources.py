"""Pavel's formula-source cases, tested without live name-resolution services."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from docx import Document
from molmass import Formula

from checksi.analysis.cross_checks import reconcile_compositions, source_composition
from checksi.config import ConfigManager
from checksi.controller import CheckSIController
from checksi.document_parser import DocumentParser
from checksi.errors import ErrorCode
from checksi.errors.manager import get_error_severity
from checksi.config_schema import ErrorSeverity
from checksi.report_rendering import _formula_html
from checksi.utils.error_handler import ErrorTracker
from tests.test_formula_reconciliation import EA_ETHANOL, EA_PROPANOL, HRMS, NMR
from tests.test_hrms import make_config


CHLORO = "Clc1ccc(NC[C@H]2C[C@@H](CN(N)c3ccc(Cl)cc3)CC2)cc1"
BROMO = CHLORO.replace("Clc1", "Brc1", 1)


class FormulaSourceReviewTests(unittest.TestCase):
    def evaluate(self, text, smiles="CCCO", overrides=None):
        tracker = ErrorTracker()
        result = CheckSIController(make_config(overrides=overrides))._analyze_single_description(
            text, smiles, tracker)
        return result, tracker

    def isotope_block(self, formula):
        mass = Formula(formula).monoisotopic_mass - 0.000548579909065
        return f"APCI [M+H]+ calcd for {formula}+ {mass:.4f}; found {mass:.4f}."

    def test_catalogue_chlorine_label_keeps_name_check_without_review_noise(self):
        block = self.isotope_block("C19H24[35Cl]2N3")
        for token, smiles, mismatches in (("[35Cl]", CHLORO, 0), ("[35Cl]", BROMO, 1),
                                          ("35Cl", CHLORO, 0), ("35Cl", BROMO, 1)):
            with self.subTest(token=token, smiles=smiles):
                block = self.isotope_block("C19H24[35Cl]2N3").replace("[35Cl]", token)
                result, tracker = self.evaluate(block, smiles)
                self.assertEqual(tracker.get_count("formula_source_review_required"), 0)
                self.assertEqual(tracker.get_count("name_formula_mismatch"), mismatches)
                self.assertEqual(tracker.get_count("formula_source_mismatch"), 0)
                self.assertEqual(result["formula_sources"]["hrms_neutral"]["composition"]["counts"]["35Cl"], 2)

    def test_enrichment_labels_remain_identity_evidence(self):
        for token, smiles in (("C19H23D[35Cl]2N3", CHLORO),
                              ("C2H9[13C]O", "CCCO"),
                              ("C3H9[18O]", "CCCO"),
                              ("C2H8[15N]", "CCN")):
            with self.subTest(token=token):
                _, tracker = self.evaluate(self.isotope_block(token), smiles)
                self.assertEqual(tracker.get_count("name_formula_mismatch"), 1)
                self.assertEqual(tracker.get_count("formula_source_review_required"), 0)
                self.assertEqual(tracker.get_count("formula_source_mismatch"), 0)
        _, tracker = self.evaluate(self.isotope_block("C3H9[18O]"), "CCC[18OH]")
        self.assertEqual(tracker.get_count("name_formula_mismatch"), 0)

    def test_name_labels_remain_isotopic_modifications_under_pavel_q11(self):
        for smiles, token in (("CC[37Cl]", "C2H6Cl"), ("CC[81Br]", "C2H6Br")):
            with self.subTest(smiles=smiles):
                result, tracker = self.evaluate(self.isotope_block(token), smiles)
                self.assertEqual(result["formula_comparisons"][-1]["status"], "not_comparable")
                self.assertEqual(tracker.get_count("formula_source_review_required"), 1)
                self.assertEqual(tracker.get_count("name_formula_mismatch"), 0)
                self.assertEqual(tracker.get_count("name_formula_mass_delta"), 0)

    def test_protium_labels_survive_inverse_adduct_accounting(self):
        for token, ion in (("C2[1H]7O", "[M+H]+"),
                           ("C2H2[1H]5O", "[M+H]+"),
                           ("C2H7O", "[M+[1H]]+"),
                           ("C2H7O", "[M+[1H]]"),
                           ("C2H2[1H]8NO", "[M+NH4]+")):
            with self.subTest(token=token, ion=ion):
                block = self.isotope_block(token).replace("[M+H]+", ion)
                result, tracker = self.evaluate(block, "CCO")
                neutral = result["formula_sources"]["hrms_neutral"]
                self.assertTrue(neutral["comparable"])
                self.assertEqual(result["formula_comparisons"][-1]["status"], "agree")
                self.assertEqual(tracker.get_count("name_formula_mismatch"), 0)
                self.assertEqual(tracker.get_count("hrms_ion_balance_invalid"), 0)
                self.assertEqual(tracker.get_count("formula_source_review_required"), 0)
                if "[1H]" in token:
                    self.assertIn("[1H]", neutral["formula"])

    def test_owned_ions_use_the_same_reviewed_natural_label_comparison_basis(self):
        for first, second, smiles in (("C2[1H]7O", "C2H6NaO", "CCO"),
                                      ("C2H6[37Cl]", "C2H5ClNa", "CCCl")):
            with self.subTest(first=first):
                first_mass = Formula(first).monoisotopic_mass - 0.000548579909065
                second_mass = Formula(second).monoisotopic_mass - 0.000548579909065
                text = (f"HRMS [M+H]+ calcd for {first}+ {first_mass:.4f}, found {first_mass:.4f}; "
                        f"[M+Na]+ calcd for {second}+ {second_mass:.4f}, found {second_mass:.4f}.")
                result, tracker = self.evaluate(text, smiles)
                self.assertTrue(result["formula_sources"]["hrms_neutral"]["comparable"])
                self.assertEqual(result["formula_comparisons"][-1]["status"], "agree")
                self.assertEqual(tracker.get_count("formula_source_review_required"), 0)

    def test_only_reviewed_natural_isotope_labels_collapse_for_comparison(self):
        for label, element in (("35Cl", "Cl"), ("37Cl", "Cl"), ("79Br", "Br"),
                               ("81Br", "Br"), ("12C", "C"), ("1H", "H"),
                               ("14N", "N"), ("16O", "O"), ("32S", "S")):
            with self.subTest(label=label):
                def source(name, formula):
                    return source_composition(name, {"formula": formula, "usable": True},
                                              state="neutral", basis="test", reason="Test composition")
                pair, = reconcile_compositions([source("hrms_neutral", f"[{label}]2"),
                                                 source("name", f"{element}2")])
                self.assertEqual(pair.status, "agree")
                self.assertFalse(pair.review_required)

    def test_charge_authority_and_conflicts_follow_pavel_q10(self):
        for ion, suffix, conflict in (("[M+H]", "+", False), ("[M+H]+", "", False),
                                      ("[M+H]-", "+", True), ("[M+H]+", "-", True),
                                      ("[M+H]", "", False)):
            with self.subTest(ion=ion, suffix=suffix):
                text = NMR + f"HRMS {ion} calcd for C3H9O{suffix} 61.0648, found 61.0648."
                result, tracker = self.evaluate(text)
                self.assertEqual(tracker.get_count("hrms_formula_charge_missing"), int(not suffix))
                self.assertEqual(tracker.get_count("hrms_ion_charge_missing"), int(ion == "[M+H]"))
                self.assertEqual(tracker.get_count("hrms_charge_inconsistent"), int(conflict))
                self.assertEqual(tracker.get_count("formula_source_review_required"), 0)
                self.assertEqual(result["formula_comparisons"][-1]["status"],
                                 "not_comparable" if conflict else "agree")
                _, wrong_name = self.evaluate(text, "CCO")
                self.assertEqual(wrong_name.get_count("name_formula_mismatch"), int(not conflict))

    def test_known_adducts_infer_missing_charge_on_the_neutral_basis(self):
        for adduct, formula in (("H", "C3H9O"), ("Na", "C3H8NaO"),
                                ("K", "C3H8KO"), ("NH4", "C3H12NO"),
                                ("Li", "C3H8LiO")):
            with self.subTest(adduct=adduct):
                result, tracker = self.evaluate(f"HRMS [M+{adduct}] calcd for {formula}+")
                self.assertTrue(result["formula_sources"]["hrms_neutral"]["comparable"])
                self.assertEqual(tracker.get_count("formula_source_review_required"), 0)
        result, tracker = self.evaluate("HRMS [M-H] calcd for C3H7O-")
        self.assertTrue(result["formula_sources"]["hrms_neutral"]["comparable"])
        self.assertEqual(tracker.get_count("formula_source_review_required"), 0)

    def test_unrecoverable_formula_source_review_defaults_to_info_and_honors_override(self):
        self.assertIs(get_error_severity(ConfigManager.load_factory(), ErrorCode.FORMULA_SOURCE_REVIEW_REQUIRED),
                      ErrorSeverity.INFO)
        for level in ("disabled", "critical"):
            self.assertEqual(get_error_severity(make_config(overrides={"formula_source_review_required": level}),
                                               ErrorCode.FORMULA_SOURCE_REVIEW_REQUIRED).value, level)
        _, tracker = self.evaluate(NMR + "HRMS calcd for C3H9O+ 61.0648, found 61.0648.")
        self.assertEqual(tracker.get_count("formula_source_review_required"), 1)

    def test_ea_hrms_pair_is_the_only_new_mismatch_pair(self):
        result, tracker = self.evaluate(NMR + HRMS + EA_ETHANOL)
        self.assertEqual(tracker.get_count("formula_source_mismatch"), 1)
        warning, = [x for x in result["warnings"] if "formula_source_mismatch." in x]
        for text in ("HRMS", "EA", "C3H9O", "C3H8O", "C2H6O"):
            self.assertIn(text, warning)
        self.assertEqual(tracker.get_count("name_formula_mismatch"), 0)

    def test_formula_table_is_absent_for_agreement_and_present_for_formula_findings(self):
        result, _ = self.evaluate(NMR + HRMS + EA_PROPANOL)
        self.assertEqual(_formula_html(result), "")
        result, _ = self.evaluate(NMR + HRMS + EA_ETHANOL)
        self.assertIn("Formula source evidence", _formula_html(result))

    def test_invalid_ea_fraction_and_unknown_hrms_symbol_have_one_precise_code(self):
        for block, expected in (("Anal. Calcd for C2.1H26N2: C, 81.77; H, 8.50; N, 9.08.",
                                  "formula_invalid_format"),
                                 ("HRMS [M+H]+ calcd for C21H29Ng3+ 100.0000, found 100.0000.",
                                  "formula_atom_symbol_invalid")):
            with self.subTest(block=block):
                result, tracker = self.evaluate(NMR + block, None)
                self.assertEqual(tracker.get_count(expected), 1)
                other = "formula_atom_symbol_invalid" if expected == "formula_invalid_format" else "formula_invalid_format"
                self.assertEqual(tracker.get_count(other), 0)
                self.assertFalse(any("Formula not checked" in line for line in result["warnings"]))


class FormulaSourceDocumentTests(unittest.TestCase):
    def test_both_formula_sources_reach_analysis_in_either_order_and_across_paragraphs(self):
        layouts = ((NMR + HRMS + EA_ETHANOL,), (NMR + EA_ETHANOL + HRMS,),
                   (NMR + HRMS, EA_ETHANOL), (NMR + EA_ETHANOL, HRMS))
        for sections in layouts:
            with self.subTest(sections=sections), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "sources.docx"
                document = Document()
                document.add_paragraph("propan-1-ol (T1)")
                for text in sections:
                    document.add_paragraph(text)
                document.save(path)
                parsed = DocumentParser().parse(str(path), header_margin=0, footer_margin=0)
                with patch("checksi.controller._smiles_and_name_from_paragraph",
                           return_value=("CCCO", "propan-1-ol", "T1", {}, [])):
                    result = CheckSIController(make_config()).run_analysis(parsed.paragraphs)
                self.assertEqual(result["total_found"], 1)
                sources = result["compounds"][0]["formula_sources"]
                self.assertTrue(sources["ea"]["usable"])
                self.assertTrue(sources["hrms_neutral"]["usable"])
                self.assertEqual(result["error_stats"]["formula_source_mismatch"], 1)
                self.assertEqual(result["assessment"]["status"], "complete")

    def test_formula_continuation_never_crosses_a_new_compound_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "two.docx"
            document = Document()
            for text in ("propan-1-ol (T1)", NMR + HRMS, "ethanol (T2)", NMR + EA_ETHANOL):
                document.add_paragraph(text)
            document.save(path)
            parsed = DocumentParser().parse(str(path), header_margin=0, footer_margin=0)
            selected = [text for text in parsed.paragraphs if "1H NMR" in text]
            self.assertEqual(len(selected), 2)
            self.assertNotIn("Anal.", selected[0])
            self.assertNotIn("HRMS", selected[1])
