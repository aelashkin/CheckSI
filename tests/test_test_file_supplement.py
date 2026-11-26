"""End-to-end check of data/checkSI_test_file_supplement.docx.

The supplement adds one example for each check the main test file does not reach in a
default run: the optional 1H CASCADE checks (multiplicity, signal outlier, unsupported
iodine, bromine evaluated as chlorine), configuration-dependent 13C counts, a 13C count
above the formula, an HRMS/EA formula conflict and an isotope EA mismatch. Structures are
given as SMILES so that the test does not depend on name resolution.
"""

import unittest
from pathlib import Path

from checksi.config import ConfigManager
from checksi.controller import CheckSIController
from checksi.errors.findings import Finding
from checksi.errors.run_diagnostics import RunDiagnostics
from checksi.file_io import paragraphs_from_input
from checksi.h1_enrichment import enrich_analysis_with_h1_shifts

TEST_FILE = Path(__file__).resolve().parents[1] / "data" / "checkSI_test_file_supplement.docx"
H1_CODES = {"h1_shift_structure_mismatch", "h1_shift_signal_outlier", "h1_shift_not_applicable",
            "h1_multiplicity_structure_mismatch", "h1_protons_unaccounted"}
CASES = [
    # identifier, SMILES (None: the name is deliberately unresolvable), expected text findings, expected 1H findings
    ("S1, correct", "CCOC(=O)c1ccccc1", set(), set()),
    ("S2", "CCOC(=O)c1ccccc1", set(), {"h1_multiplicity_structure_mismatch"}),
    ("S3", "CC(=O)OC(C)c1ccccc1", set(), {"h1_multiplicity_structure_mismatch"}),
    ("S4", "CCOC(=O)c1ccccc1", set(), {"h1_shift_structure_mismatch", "h1_shift_signal_outlier",
                                        "h1_multiplicity_structure_mismatch"}),
    ("S5", "Cc1ccc(Br)cc1", set(), set()),
    ("S6", "Cc1ccc(I)cc1", set(), {"h1_shift_not_applicable"}),
    ("S7", "CC(O)C(C)C(C)O", {"nmr_configuration_dependent"}, set()),
    ("S8", None, {"c13_count_exceeds_formula"}, set()),
    ("S9", "OCc1ccccc1", {"formula_source_mismatch"}, set()),
    ("S10", None, {"ea_isotope_calcd_mismatch"}, set()),
    ("S11", "CCOC(=O)c1ccccc1", set(), {"h1_shift_structure_mismatch"}),
    ("S12", "CC(=O)OC(C)c1ccccc1", set(), {"h1_shift_signal_outlier"}),
]


def descriptions_by_identifier():
    paragraphs = [p for p in paragraphs_from_input(str(TEST_FILE), header_margin=0, footer_margin=0) if p.strip()]
    return {previous.rstrip()[:-1].rsplit("(", 1)[1]: paragraph
            for previous, paragraph in zip(paragraphs, paragraphs[1:])
            if previous.rstrip().endswith(")") and not paragraph.rstrip().endswith(")")}


class TestFileSupplementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.descriptions = descriptions_by_identifier()
        cls.config = ConfigManager.load_factory()
        cls.config.h1_shift.enabled = True

    def test_every_case_is_present(self):
        self.assertEqual(sorted(self.descriptions), sorted(case[0] for case in CASES))

    def test_each_case_reports_its_target_findings(self):
        for identifier, smiles, text_codes, h1_codes in CASES:
            with self.subTest(identifier=identifier):
                diagnostics = RunDiagnostics(self.config)
                result = CheckSIController(self.config)._analyze_description_diagnostics(
                    self.descriptions[identifier], smiles, diagnostics.tracker)
                found = {str(item.code.value) for item in result["warnings"] if isinstance(item, Finding)}
                self.assertLessEqual(text_codes, found)
                if smiles is None:
                    continue
                compound = {"id": 1, "smiles": smiles, "all_warnings": [],
                            "highlight_info": {"hnmr_text": result["hnmr_section_text"]}}
                data = {"compounds": [compound]}
                enrich_analysis_with_h1_shifts(self.config, data)
                self.assertEqual({code for code, count in data["error_stats"].items() if count and code in H1_CODES},
                                 h1_codes)


if __name__ == "__main__":
    unittest.main()
