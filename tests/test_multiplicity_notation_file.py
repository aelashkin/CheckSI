"""End-to-end check of data/checkSI_multiplicity_notation_test.docx.

Compounds 1-9 use valid notations (quint, sext, sept, app/pseudo prefixes, labelled J such as
3JH-H, JC-F, JCF, J(H,F), J_{HF}) and must give no notation findings. Compounds 10-11 are
deliberate errors that must still be reported. Structures are given as SMILES so that the test
does not depend on name resolution.
"""

import unittest
from pathlib import Path

from checksi.config import ConfigManager
from checksi.controller import CheckSIController
from checksi.errors.findings import Finding
from checksi.errors.run_diagnostics import RunDiagnostics
from checksi.file_io import paragraphs_from_input

TEST_FILE = Path(__file__).resolve().parents[1] / "data" / "checkSI_multiplicity_notation_test.docx"
NOTATION_CODES = {
    "hnmr_multiplicity_missing", "hnmr_wrong_multiplicity", "hnmr_j_missing", "hnmr_signal_desc_invalid",
    "nmr_multiplicity_missing", "nmr_wrong_multiplicity", "nmr_j_missing", "nmr_signal_desc_invalid",
    "c13_coupling_missing_with_f",
}
VALID = [
    ('1', 'CCCC(=O)c1ccccc1'),
    ('2', 'BrCCCc1ccccc1'),
    ('3', 'CC(C)c1ccccc1'),
    ('4', 'CC(=O)CCc1ccccc1'),
    ('5', 'O=Cc1ccc(F)cc1'),
    ('6', 'CC(=O)c1ccc(F)cc1'),
    ('7', 'CCOP(=O)(Cc1ccccc1)OCC'),
    ('8', 'FCCc1ccccc1'),
    ('9', 'Cc1ccc(C(F)(F)F)cc1'),
]
INVALID = [
    ('10', 'CCCC(=O)c1ccccc1', ['hnmr_j_missing']),
    ('11', 'O=Cc1ccc(F)cc1', ['hnmr_wrong_multiplicity', 'nmr_j_missing']),
]


def descriptions_by_identifier():
    paragraphs = [p for p in paragraphs_from_input(str(TEST_FILE), header_margin=0, footer_margin=0) if p.strip()]
    found = {}
    for previous, paragraph in zip(paragraphs, paragraphs[1:]):
        if paragraph.startswith("1H NMR") and previous.rstrip().endswith(")"):
            found[previous.rstrip()[:-1].rsplit("(", 1)[1]] = paragraph
    return found


class MultiplicityNotationFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.descriptions = descriptions_by_identifier()
        cls.config = ConfigManager.load_factory()

    def codes(self, identifier, smiles):
        diagnostics = RunDiagnostics(self.config)
        result = CheckSIController(self.config)._analyze_description_diagnostics(
            self.descriptions[identifier], smiles, diagnostics.tracker)
        return {str(item.code.value) for item in result["warnings"] if isinstance(item, Finding)}

    def test_file_contains_all_compounds(self):
        self.assertEqual(set(self.descriptions), {ident for ident, _ in VALID} | {ident for ident, *_ in INVALID})

    def test_valid_notations_give_no_notation_findings(self):
        for identifier, smiles in VALID:
            with self.subTest(compound=identifier):
                codes = self.codes(identifier, smiles)
                self.assertEqual(codes & NOTATION_CODES, set())
                self.assertNotIn("h_count_mismatch", codes)

    def test_deliberate_errors_are_still_reported(self):
        for identifier, smiles, expected in INVALID:
            with self.subTest(compound=identifier):
                self.assertTrue(set(expected) <= self.codes(identifier, smiles))


if __name__ == "__main__":
    unittest.main()
