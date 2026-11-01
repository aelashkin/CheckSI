"""Public-corpus identity/label evidence and inert dataset decoding checks."""
import importlib.util
from collections import Counter
import json
from pathlib import Path
import pickle
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from rdkit import Chem

SPEC = importlib.util.spec_from_file_location("dp5q_audit", Path(__file__).parents[1] / "scripts/audit_dp5q_corpus.py")
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)
FIXTURES = Path(__file__).parent / "fixtures/dp5q"


class ExecutablePickle:
    """Authored malicious pickle control, never deserialized by this test."""
    def __reduce__(self):
        import os
        return os.system, ("false",)


class CorpusAuditTests(unittest.TestCase):
    def test_dataset_opcodes_never_execute_reconstruction(self):
        payload = pickle.dumps([Chem.MolFromSmiles("CCO").ToBinary(), ExecutablePickle()])
        with patch("os.system") as execute:
            molecules = audit.molecular_literals(payload)
        execute.assert_not_called()
        self.assertEqual([Chem.MolToSmiles(mol) for mol in molecules], ["CCO"])

    def test_wrong_dataset_identity_fails_before_opcode_or_molecule_parsing(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "nmrdb-dataset").mkdir()
            (source / "nmrdb-dataset/train_NMRdb-CASCADEset.pkl.gz").write_bytes(b"untrusted")
            with patch.object(audit, "molecular_literals") as decode, self.assertRaises(ValueError):
                audit.load_split(source, "train")
            decode.assert_not_called()

    def test_committed_recount_excludes_conflicting_labels_and_exposes_duplicates(self):
        manifest = json.loads((FIXTURES / "manifest.json").read_text())
        result = json.loads((FIXTURES / "development-audit.json").read_text())
        ledger = json.loads((FIXTURES / "historical-sdf-results.json").read_text())
        self.assertEqual(result["manifest_sha256"], audit.sha256((FIXTURES / "manifest.json").read_bytes()).hexdigest())
        self.assertEqual(result["historical_results_sha256"], audit.sha256((FIXTURES / "historical-sdf-results.json").read_bytes()).hexdigest())
        self.assertEqual((result["candidate_count"], result["distinct_exact_structures"], result["duplicate_candidate_count"]), (370, 338, 32))
        cases = {case["case_id"]: case for case in manifest["cases"]}
        first, second = cases["stereo/JB13A_1"], cases["stereo/JB13B_1"]
        self.assertEqual(first["sdf"]["sha256"], second["sdf"]["sha256"])
        self.assertNotEqual(manifest["spectra"][first["spectrum_id"]]["carbon_shifts"],
                            manifest["spectra"][second["spectrum_id"]]["carbon_shifts"])
        self.assertEqual(sum(not row["label_eligible"] for row in result["cases"]), 8)
        recomputed = audit.historical_metrics(ledger, result["cases"])
        self.assertEqual(recomputed, result["historical_sdf_order_metrics"])
        b = recomputed["0.1"]["all_eligible_labels"]["B"]
        self.assertEqual((b["correct"]["submitted"], b["incorrect"]["submitted"]), (64, 298))
        self.assertEqual((b["correct"]["assessed"], b["incorrect"]["assessed"], b["incorrect"]["compatible"]), (7, 10, 9))
        self.assertEqual(Counter(row["direction"] for row in result["coverage_mismatches"] if row["shared_family_spectrum"]),
                         {"too_few": 7, "too_many": 5})

    def test_every_split_and_union_are_reported_without_holdout_claim(self):
        result = json.loads((FIXTURES / "development-audit.json").read_text())
        self.assertEqual({name: value["candidate_constitution"] for name, value in result["overlap_by_split"].items()},
                         {"train": 78, "valid": 0, "test": 50, "all_provided_splits": 94})
        self.assertTrue(all(row["all_provided_splits_constitution"] for row in result["bmrb_depictions"]))
        self.assertFalse(next(row for row in result["bmrb_depictions"] if row["accession"] == "bmse000967")["train_constitution"])

    def test_byte_bound_evidence_disables_checkout_line_ending_conversion(self):
        paths = ["tests/fixtures/dp5q/manifest.json", "tests/fixtures/dp5q/historical-sdf-results.json"]
        output = subprocess.run(["git", "-c", "core.autocrlf=true", "check-attr", "-z", "text", "--stdin"],
                                input=("\0".join(paths) + "\0").encode(), cwd=FIXTURES.parents[2],
                                capture_output=True, check=True)
        expected = [field for path in paths for field in (path.encode(), b"text", b"unset")] + [b""]
        self.assertEqual(output.stdout.split(b"\0"), expected)


if __name__ == "__main__":
    unittest.main()
