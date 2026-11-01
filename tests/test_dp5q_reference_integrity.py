"""Reference integrity checks remain pure and independent of TensorFlow/POSIX."""
from __future__ import annotations

import builtins
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import pickle
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/dp5q_reference.py"


def load_reference_script():
    spec = importlib.util.spec_from_file_location("reference_integrity_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        with patch.dict(os.environ):
            spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


class ReferenceIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.reference = load_reference_script()
        directory = tempfile.TemporaryDirectory(prefix="dp5q-reference-pin-")
        self.addCleanup(directory.cleanup)
        self.source = Path(directory.name)
        self.contents = {"dp5/__init__.py": b"", "dp5/module.py": b"VALUE = 1\n",
                         "dp5/neural_net/mean_model_preprocessor.p": pickle.dumps({"fixture": True}),
                         "license.md": b"Fixture license\n"}
        for name, data in self.contents.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.pin = {"source_revision": self.reference.SOURCE_REVISION,
                    "files": {name: {"bytes": len(data), "sha256": sha256(data).hexdigest()}
                              for name, data in self.contents.items()}}

    def test_reference_module_import_needs_no_tensorflow_or_posix_resource(self):
        original = builtins.__import__
        def guarded(name, *args, **kwargs):
            if name in ("tensorflow", "resource"):
                raise AssertionError("Pure integrity import reached " + name)
            return original(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=guarded):
            self.assertIsNotNone(load_reference_script())

    def test_complete_pinned_source_passes_without_executing_it(self):
        with patch.object(self.reference, "_source_pin", return_value=self.pin):
            result = self.reference.verify_reference_source(self.source)
        self.assertEqual(result, {name: row["sha256"] for name, row in self.pin["files"].items()})

    def test_modified_source_extra_python_and_bytecode_are_refused(self):
        with patch.object(self.reference, "_source_pin", return_value=self.pin):
            for name in ("dp5/extra.py", "dp5/__pycache__/module.cpython-311.pyc"):
                path = self.source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"untrusted")
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, "pinned"):
                    self.reference.verify_reference_source(self.source)
                path.unlink()
            (self.source / "dp5/module.py").write_bytes(b"VALUE = 2\n")
            with self.assertRaisesRegex(ValueError, "pinned"):
                self.reference.verify_reference_source(self.source)

    def test_asset_tampering_is_refused_before_tensorflow_or_pickle(self):
        path = self.source / "dp5/neural_net/mean_model_preprocessor.p"
        data = path.read_bytes()
        path.write_bytes(b"x" + data[1:])
        original = builtins.__import__
        def guarded(name, *args, **kwargs):
            if name == "tensorflow":
                raise AssertionError("Tampered source reached TensorFlow")
            return original(name, *args, **kwargs)
        with patch.object(self.reference, "_source_pin", return_value=self.pin), \
                patch("builtins.__import__", side_effect=guarded), \
                patch.object(self.reference.pickle, "loads") as unsafe:
            with self.assertRaisesRegex(ValueError, "pinned"):
                self.reference.load_reference(self.source)
            unsafe.assert_not_called()

    def test_explicit_pickle_load_uses_verified_bytes(self):
        with patch.object(self.reference, "_source_pin", return_value=self.pin), patch.dict(sys.modules):
            self.assertEqual(self.reference.load_verified_preprocessor(self.source), {"fixture": True})
            path = self.source / "dp5/neural_net/mean_model_preprocessor.p"
            path.write_bytes(path.read_bytes() + b"changed")
            with patch.object(self.reference.pickle, "loads") as unsafe:
                with self.assertRaisesRegex(ValueError, "pinned"):
                    self.reference.load_verified_preprocessor(self.source)
                unsafe.assert_not_called()

    def test_verified_package_binding_does_not_expose_checkout_root(self):
        (self.source / "numpy.py").write_text("raise AssertionError('unverified root import')\n")
        paths = list(sys.path)
        with patch.dict(sys.modules):
            self.reference._bind_reference_package(self.source)
            self.assertEqual(sys.path, paths)
            self.assertEqual(Path(sys.modules["dp5"].__file__).resolve(),
                             (self.source / "dp5/__init__.py").resolve())
            self.assertFalse((self.source / "dp5/__pycache__").exists())

    def test_committed_source_pin_covers_executed_code_and_original_assets(self):
        pin = self.reference._source_pin()
        manifest = json.loads((ROOT / "src/checksi/dp5q/data/manifest.json").read_text())
        self.assertEqual(pin["source_revision"], manifest["provenance"]["source_revision"])
        for name, digest in manifest["provenance"]["original_asset_hashes"].items():
            self.assertEqual(pin["files"][name]["sha256"], digest)
        self.assertIn("dp5/neural_net/CNN_model.py", pin["files"])
        self.assertIn("dp5/analysis/dp5.py", pin["files"])
        self.assertIn("license.md", pin["files"])

    def test_every_reference_dependency_is_pinned_and_hashed(self):
        for name, expected_count in (("requirements.lock", 54), ("requirements-cli.lock", 67)):
            text = (ROOT / "scripts/dp5q-reference" / name).read_text()
            self.assertIn("--require-hashes", text)
            blocks = re.split(r"(?m)^(?=[A-Za-z0-9][A-Za-z0-9_.-]*==)", text)[1:]
            self.assertEqual(len(blocks), expected_count)
            for block in blocks:
                self.assertRegex(block, r"--hash=sha256:[0-9a-f]{64}")
            self.assertRegex(text, r"(?m)^tensorflow==2\.14\.0(?:\s|$)")
            self.assertRegex(text, r"(?m)^numpy==1\.26\.4(?:\s|$)")
