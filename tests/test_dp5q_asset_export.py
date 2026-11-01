"""Re-export existing safe weights with authenticated provenance, without an oracle."""

from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch


class AssetExportTests(unittest.TestCase):
    """The test receipt exercises export plumbing, not scientific reference parity."""

    @classmethod
    def setUpClass(cls):
        cls.project = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("dp5q_asset_export", cls.project / "scripts/export_dp5q_assets.py")
        cls.exporter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.exporter)

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="dp5q-export-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.reference = self.root / "reference"
        self.source = self.root / "source"
        self.output = self.root / "output"
        self.reference.mkdir()
        self.source.mkdir()
        assets = self.project / "src/checksi/dp5q/data"
        self.mapping = self.root / "tensor-map.json"
        self.mapping.write_bytes((self.project / "scripts/dp5q-reference/tensor-map.json").read_bytes())
        rows = json.loads(self.mapping.read_bytes())["rows"]
        metadata = json.loads((assets / "manifest.json").read_bytes())
        receipt = {
            "source_revision": metadata["provenance"]["source_revision"], "source_unchanged": True,
            "shared_count": 41, "unique_count": 57, "raw_unique_bytes": 11_625_924,
            "fixtures": [{"shared_activations": {"test-stage": {"exact": True}}}] * 2,
            "batch_count": 16, "batch_parity": "PASS atol=1e-3ppm rtol=0",
            "semantic_tensors": [{"layer": row["semantic_layer"],
                "kind": {"embeddings:0": "embedding", "kernel:0": "kernel", "bias:0": "bias"}[row["mean_key"].rsplit("/", 1)[-1]],
                "shared": row["byte_identical"], **{key: row[key] for key in
                    ("mean_sha256", "quantile_sha256", "mean_shape", "quantile_shape")}} for row in rows],
            "source_hashes": metadata["provenance"]["original_asset_hashes"],
            "tokens": metadata["tokens"], "quantile_levels": metadata["quantile_levels"],
        }
        (self.reference / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        shutil.copyfile(assets / "weights.npz", self.reference / "semantic-weights.npz")
        shutil.copyfile(assets / "LICENSE.dp5.txt", self.source / "license.md")

    def export(self):
        argv = ["export_dp5q_assets.py", "--reference", str(self.reference), "--mapping", str(self.mapping),
                "--source", str(self.source), "--output", str(self.output)]
        with patch.object(sys, "argv", argv):
            self.exporter.main()

    def test_export_uses_exact_authoritative_notice_and_records_every_notice_hash(self):
        self.export()
        assets = self.project / "src/checksi/dp5q/data"
        self.assertEqual((self.output / "NOTICE.txt").read_bytes(), (assets / "NOTICE.txt").read_bytes())
        manifest = json.loads((self.output / "manifest.json").read_bytes())
        expected = {path.name for path in self.output.glob("*.txt")}
        self.assertEqual(set(manifest["provenance"]["notices"]), expected)
        for name, info in manifest["provenance"]["notices"].items():
            data = (self.output / name).read_bytes()
            self.assertEqual(len(data), info["bytes"])
            self.assertEqual(sha256(data).hexdigest(), info["sha256"])

    def test_export_refuses_changed_source_license_before_creating_output(self):
        with (self.source / "license.md").open("ab") as stream:
            stream.write(b"altered notice\n")
        with self.assertRaisesRegex(ValueError, "license"):
            self.export()
        self.assertFalse(self.output.exists())

    def test_export_refuses_byte_changed_tensor_map_before_creating_output(self):
        with self.mapping.open("ab") as stream:
            stream.write(b"\n")
        with self.assertRaisesRegex(ValueError, "tensor map"):
            self.export()
        self.assertFalse(self.output.exists())

    def test_tracked_tensor_map_is_the_exact_historical_identity_map(self):
        expected = "76d81ae00ba3cd91f1243fd8df616a0967e1dc41eda8d84c9462ea09c91177a4"
        self.assertEqual(sha256(self.mapping.read_bytes()).hexdigest(), expected)
        metadata = json.loads((self.project / "src/checksi/dp5q/data/manifest.json").read_bytes())
        self.assertEqual(metadata["provenance"]["tensor_map_sha256"], expected)
