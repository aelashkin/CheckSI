"""Guard installed-artifact evidence against missing resources and masked findings."""

import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
import sys
from zipfile import ZipFile
from unittest.mock import patch


def checker_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "portability_check.py"
    spec = importlib.util.spec_from_file_location("portability_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PortabilityEvidenceTests(unittest.TestCase):
    """Packaging evidence must include the actual resource bytes and all analytical data."""

    def setUp(self):
        self.checker = checker_module()

    def test_semantic_projection_retains_text_numeric_findings_and_severities(self):
        analysis = {"total_found": 1, "compounds": [{"original_text": "δ 1.20", "mass": 47.0491}],
                    "error_stats": {"hrms_ppm_too_large": 2}}
        payload = {"schema_version": 2, "analysis": analysis,
                   "finding_severities": {"hrms_ppm_too_large": "warning"},
                   "source": {"path": "/one/input.pdf"}, "export": {"html_path": "/one/report.html"}}
        projection = self.checker.semantic_projection(payload)
        self.assertEqual(projection, {"schema_version": 2, "analysis": analysis,
                                     "finding_severities": payload["finding_severities"]})
        payload["analysis"]["compounds"][0]["original_text"] = "different text"
        self.assertNotEqual(projection, self.checker.semantic_projection(payload))

    def test_resources_must_match_both_wheel_and_sdist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "checksi"
            files = {"defaults.toml": b"defaults", "gui/assets/checksi_logo.png": b"icon",
                     "resources/fonts/NotoSans-Regular.ttf": b"regular",
                     "resources/fonts/NotoSans-Bold.ttf": b"bold",
                     "resources/fonts/LICENSE.txt": b"license", "resources/fonts/SOURCE.txt": b"provenance"}
            for name, content in files.items():
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            wheel, sdist = root / "package.whl", root / "package.tar.gz"
            with ZipFile(wheel, "w") as archive:
                for name, content in files.items():
                    archive.writestr("checksi/" + name, content)
            with tarfile.open(sdist, "w:gz") as archive:
                for name, content in files.items():
                    info = tarfile.TarInfo("package-1/src/checksi/" + name)
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
            self.assertEqual(set(self.checker.validate_resources(wheel, sdist, source)), set(files))
            with ZipFile(wheel, "w") as archive:
                for name, content in files.items():
                    archive.writestr("checksi/" + name, b"changed" if name == "defaults.toml" else content)
            with self.assertRaisesRegex(ValueError, "resource"):
                self.checker.validate_resources(wheel, sdist, source)

    def test_annotation_projection_retains_identity_phrase_and_color_across_layouts(self):
        first = [{"compound_id": 1, "phrase": "δ 1.20", "color": "red", "page_1based": 1, "rect": [1, 2, 3, 4]}]
        second = [{"compound_id": 1, "phrase": "δ 1.20", "color": "red", "page_1based": 2, "rect": [5, 6, 7, 8]}]
        self.assertEqual(self.checker.annotation_projection(first), self.checker.annotation_projection(second))
        second[0]["compound_id"] = 2
        self.assertNotEqual(self.checker.annotation_projection(first), self.checker.annotation_projection(second))

    def test_constraints_export_keeps_gui_pins_without_obsolete_com_or_dev_groups(self):
        constraints = self.checker.locked_constraints(Path(__file__).resolve().parents[1])
        lines = constraints.splitlines()
        self.assertTrue(any(line.startswith("pyside6==") for line in lines))
        self.assertFalse(any(line.startswith("comtypes==") for line in lines))
        self.assertFalse(any("graphify" in line or line.startswith(("-e ", "checksi==")) for line in lines))

    def test_generated_docx_fixture_reaches_real_hrms_and_elemental_analysis(self):
        from docx import Document
        from checksi.config import ConfigManager
        from checksi.controller import CheckSIController
        from checksi.file_io import paragraphs_from_input
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "fixture α.docx"
            document = Document()
            document.styles["Normal"].font.name = "Arial"
            document.add_paragraph(self.checker.FIXTURE)
            document.save(source)
            paragraphs = paragraphs_from_input(str(source), header_margin=0, footer_margin=0)
            config = ConfigManager.load_factory()
            config.dp5.enabled = False
            with patch("checksi.controller._smiles_and_name_from_paragraph", return_value=(None, None, None, {}, [])):
                result = CheckSIController(config, event_dispatcher=lambda event: None).run_analysis(paragraphs)
        self.assertEqual(result["total_found"], 2)
        self.assertEqual(result["error_stats"]["hrms_ppm_too_large"], 2)
        self.assertEqual(result["error_stats"]["ea_tolerance_exceeded"], 1)

    def test_findings_pdf_requires_actual_noto_family_names_and_scientific_text(self):
        fonts = ["Noto Sans Bold", "Noto Sans Regular"]
        self.checker.validate_findings_pdf("1H NMR δ", fonts)
        with self.assertRaisesRegex(RuntimeError, "font"):
            self.checker.validate_findings_pdf("1H NMR δ", ["Times Roman"])
        with self.assertRaisesRegex(RuntimeError, "Unicode"):
            self.checker.validate_findings_pdf("1H NMR �", fonts)

    def test_subprocess_protocol_reads_utf8_independently_of_windows_locale(self):
        with tempfile.TemporaryDirectory() as directory, patch("locale.getencoding", return_value="cp1252"):
            result = self.checker.run([sys.executable, "-c", "import sys; sys.stdout.buffer.write(bytes([206,180]))"],
                                      cwd=Path(directory))
        self.assertEqual(result.stdout, "δ")

    def test_owned_isolated_python_protocol_ignores_cp1252_environment(self):
        environment = {**os.environ, "PYTHONIOENCODING": "cp1252:strict", "PYTHONUTF8": "0"}
        code = "import json,sys;print(json.dumps([sys.flags.isolated,sys.flags.utf8_mode,'café α'],ensure_ascii=False))"
        with tempfile.TemporaryDirectory() as directory:
            result = self.checker.run(self.checker.isolated_python(Path(sys.executable), "-c", code),
                                      cwd=Path(directory), env=environment)
        self.assertEqual(json.loads(result.stdout), [1, 1, "café α"])

    def test_required_converter_cannot_fall_back_to_partial_when_missing(self):
        for executable in (None, "", "/definitely/missing/libreoffice"):
            with self.subTest(executable=executable), self.assertRaisesRegex(ValueError, "required LibreOffice"):
                self.checker.require_converter(executable, required=True)
        self.checker.require_converter(None, required=False)

    def test_cli_required_converter_fails_before_reading_artifacts_or_installing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = [sys.executable, str(Path(self.checker.__file__)), "verify", "--require-libreoffice",
                       "--wheel", str(root / "missing.whl"), "--sdist", str(root / "missing.tar.gz"),
                       "--wheelhouse", str(root / "missing-wheelhouse")]
            result = self.checker.run(command, cwd=root, expected=(1,))
            self.assertIn("required LibreOffice", result.stderr)

    def test_required_native_lane_receipt_needs_actual_complete_docx_conversion(self):
        receipt = {"platform": "Linux-6-x86_64", "python": "3.13.16", "machine": "x86_64",
                   "cases": [{"case": "docx", "partial": True}], "required_libreoffice": True}
        with self.assertRaisesRegex(ValueError, "required LibreOffice"):
            self.checker.validate_native_converters([receipt])
        receipt["libreoffice"] = {"version": "LibreOffice 26.8.1.2"}
        receipt["cases"][0]["partial"] = False
        self.checker.validate_native_converters([receipt])

    def compare_native_receipts(self, receipts):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, receipt in enumerate(receipts):
                (root / f"portability-{index}.json").write_text(json.dumps(receipt), encoding="utf-8")
            return self.checker.run(
                [sys.executable, self.checker.__file__, "compare", "--receipts", str(root)],
                cwd=root, expected=(0, 1))

    def native_matrix_receipts(self):
        receipts = []
        for platform, machine in (("Linux-6-x86_64", "x86_64"), ("Windows-11-AMD64", "AMD64")):
            for python in ("3.12.12", "3.13.16"):
                required = platform.startswith("Linux") and python.startswith("3.13.")
                receipts.append({
                    "kind": "base", "platform": platform, "machine": machine, "python": python,
                    "required_libreoffice": required,
                    "libreoffice": {"version": "LibreOffice 26.8.1.2"} if required else None,
                    "cases": [{"case": label, "partial": label == "docx" and not required,
                               "semantics": {"analysis": {"total_found": 1}},
                               "source_associations": [{"compound_id": 1, "phrase": "δ 1.20", "color": "red"}]}
                              for label in ("pdf", "docx")],
                })
        return receipts

    def test_native_comparison_accepts_complete_linux_windows_matrix(self):
        result = self.compare_native_receipts(self.native_matrix_receipts())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["native_receipts"], 4)
        self.assertTrue(json.loads(result.stdout)["semantics_equal"])

    def test_native_comparison_rejects_missing_lane_even_with_duplicate_receipts(self):
        receipts = self.native_matrix_receipts()
        for missing in range(len(receipts)):
            with self.subTest(missing=missing):
                incomplete = [receipt for index, receipt in enumerate(receipts) if index != missing]
                result = self.compare_native_receipts(incomplete + incomplete)
                self.assertEqual(result.returncode, 1)
                self.assertIn("Linux/Windows Python 3.12/3.13", result.stderr)

    def test_native_comparison_keeps_converter_and_analytical_guards(self):
        receipts = self.native_matrix_receipts()
        receipts[1]["cases"][1]["partial"] = True
        result = self.compare_native_receipts(receipts)
        self.assertEqual(result.returncode, 1)
        self.assertIn("required LibreOffice", result.stderr)
        receipts = self.native_matrix_receipts()
        receipts[2]["cases"][0]["semantics"]["analysis"]["total_found"] = 2
        result = self.compare_native_receipts(receipts)
        self.assertEqual(result.returncode, 1)
        self.assertIn("differ in analytical values", result.stderr)

    def test_native_comparison_rejects_changed_annotation_mapping(self):
        receipts = self.native_matrix_receipts()
        receipts[2]["cases"][0]["source_associations"][0]["compound_id"] = 2
        result = self.compare_native_receipts(receipts)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Native pdf compound-to-phrase associations differ", result.stderr)


if __name__ == "__main__":
    unittest.main()
