"""Inspect a real offline-built wheel rather than an editable installation."""

from __future__ import annotations

from email.parser import BytesParser
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
import uuid
import venv
from pathlib import Path
from zipfile import ZipFile
from unittest.mock import patch


class DP5qPackagingTests(unittest.TestCase):
    """Build from a fresh source staging directory without old build artifacts."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory(prefix="dp5q-wheel-test-")
        cls.addClassCleanup(cls.directory.cleanup)
        stage = Path(cls.directory.name) / "source"
        stage.mkdir()
        root = Path(__file__).resolve().parents[1]
        helper_path = root / "scripts/dp5q_packaging_check.py"
        spec = importlib.util.spec_from_file_location("wheel_build_staging", helper_path)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        helper.stage_source(root, stage)
        built = subprocess.run([
            "uv", "build", "--wheel", "--no-build-isolation", "--offline",
            "--python", sys.executable, "--out-dir", str(stage / "dist"),
        ], cwd=stage, capture_output=True, text=True, timeout=90, check=False)
        if built.returncode:
            raise RuntimeError(built.stdout + built.stderr)
        cls.archive = ZipFile(next((stage / "dist").glob("*.whl")))
        cls.addClassCleanup(cls.archive.close)

    def portability_checker(self):
        path = Path(__file__).resolve().parents[1] / "scripts/portability_check.py"
        spec = importlib.util.spec_from_file_location("portable_wheel_check", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_public_install_resources_and_console_entrypoints_are_in_real_wheel(self):
        checker = self.portability_checker()
        source = Path(__file__).resolve().parents[1] / "src/checksi"
        actual = checker.validate_wheel_contract(Path(self.archive.filename), source)
        self.assertEqual(set(actual["resources"]), set(checker.RESOURCES))
        self.assertEqual(actual["console_scripts"], {"checksi": "checksi.main:entrypoint",
                                                    "checksi-gui": "checksi.gui_main:main_gui"})

    def test_missing_font_or_console_script_metadata_is_rejected_before_install(self):
        checker = self.portability_checker()
        source = Path(__file__).resolve().parents[1] / "src/checksi"
        entries = self.archive.namelist()
        entrypoints = next(name for name in entries if name.endswith(".dist-info/entry_points.txt"))
        for missing in ("checksi/resources/fonts/NotoSans-Regular.ttf", entrypoints):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                wheel = Path(directory) / Path(self.archive.filename).name
                with ZipFile(wheel, "w") as output:
                    for name in entries:
                        if name != missing:
                            output.writestr(name, self.archive.read(name))
                with self.assertRaisesRegex(ValueError, "resource|entrypoint"):
                    checker.validate_wheel_contract(wheel, source)

    def test_console_script_names_are_case_sensitive(self):
        checker = self.portability_checker()
        source = Path(__file__).resolve().parents[1] / "src/checksi"
        entries = self.archive.namelist()
        entrypoints = next(name for name in entries if name.endswith(".dist-info/entry_points.txt"))
        for command in (b"checksi", b"checksi-gui"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                original = self.archive.read(entrypoints)
                wrong_case = command.replace(b"checksi", b"CheckSI", 1)
                changed = original.replace(command + b" =", wrong_case + b" =", 1)
                self.assertNotEqual(original, changed)
                wheel = Path(directory) / Path(self.archive.filename).name
                with ZipFile(wheel, "w") as output:
                    for name in entries:
                        output.writestr(name, changed if name == entrypoints else self.archive.read(name))
                with self.assertRaisesRegex(ValueError, "entrypoint contract differs"):
                    checker.validate_wheel_contract(wheel, source)

    def test_installed_cli_runs_factory_and_pdf_offline_outside_source(self):
        """Install the real wheel, sharing existing read-only third-party runtime files."""
        with tempfile.TemporaryDirectory(prefix="wheel-cli-smoke-") as directory:
            root = Path(directory)
            environment = root / "environment"
            venv.create(environment, with_pip=False)
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}
            home = root / "home"
            home.mkdir()
            env.update(HOME=str(home), USERPROFILE=str(home), XDG_CONFIG_HOME=str(home / "xdg"),
                       LOCALAPPDATA=str(home / "local"), APPDATA=str(home / "roaming"),
                       UV_OFFLINE="1", UV_NO_CACHE="1", PYTHONDONTWRITEBYTECODE="1")
            purelib = subprocess.check_output([str(python), "-I", "-X", "utf8", "-c",
                                               "import sysconfig; print(sysconfig.get_path('purelib'))"],
                                              cwd=root, env=env, encoding="utf-8", timeout=10).strip()
            import sysconfig
            runtime = sysconfig.get_path("purelib")
            self.assertNotIn("\n", runtime)
            (Path(purelib) / "third_party_runtime.pth").write_text(runtime + "\n", encoding="utf-8")
            subprocess.run(["uv", "pip", "install", "--python", str(python), "--offline", "--no-index", "--no-deps", "--no-cache",
                            str(self.archive.filename)], cwd=root, env=env, check=True, capture_output=True, timeout=30)
            checker = self.portability_checker()
            checker.packaging_helper().install_verification_guard(Path(purelib), root / "startups.jsonl")
            origin = subprocess.check_output([str(python), "-I", "-X", "utf8", "-c",
                "from pathlib import Path; import checksi,sys; assert Path(sys.prefix).resolve() in Path(checksi.__file__).resolve().parents; print(checksi.__file__)"],
                cwd=root, env=env, encoding="utf-8", timeout=10)
            self.assertIn(str(environment.resolve()), str(Path(origin.strip()).resolve()))
            console = environment / ("Scripts/checksi.exe" if os.name == "nt" else "bin/checksi")
            viewed = checker.run([str(console), "config", "view"], cwd=root, env=env)
            self.assertIn("paths", json.loads(viewed.stdout))
            self.assertEqual(list(home.rglob("*")), [])
            import fitz
            source = root / "empty α.pdf"
            with fitz.open() as pdf:
                pdf.new_page().insert_text((72, 72), "General methods.")
                pdf.save(source)
            completed = checker.run([str(console), "run", str(source), "--output-dir", str(root / "reports"),
                                     "--override", "modes.deep_name_analysis=off", "--override", "dp5.enabled=false"],
                                    cwd=root, env=env, expected=(3,))
            payload = json.loads(next((root / "reports").glob("*/results.json")).read_text(encoding="utf-8"))
            self.assertEqual(payload["analysis"]["total_found"], 0)
            self.assertEqual(payload["analysis"]["assessment"]["status"], "none")
            self.assertFalse(payload["export"]["partial"], completed.stdout)
            for key in ("html_path", "analysis_path", "pdf_path", "error_stats_path"):
                self.assertTrue(Path(payload["export"][key]).is_file(), key)

    def test_numeric_assets_and_all_notices_are_present_and_hash_exact(self) -> None:
        prefix = "checksi/dp5q/data/"
        manifest = json.loads(self.archive.read(prefix + "manifest.json"))
        weights = self.archive.read(prefix + "weights.npz")
        self.assertEqual(len(weights), manifest["files"]["weights.npz"]["bytes"])
        self.assertEqual(hashlib.sha256(weights).hexdigest(), manifest["files"]["weights.npz"]["sha256"])
        for name in ("LICENSE.dp5.txt", "LICENSE.cascade.txt", "LICENSE.nfp.txt", "LICENSE.nmrshiftdb2.txt", "NOTICE.txt"):
            data = self.archive.read(prefix + name)
            info = manifest["provenance"]["notices"][name]
            self.assertEqual(len(data), info["bytes"], name)
            self.assertEqual(hashlib.sha256(data).hexdigest(), info["sha256"], name)
        expected = set(manifest["files"]) | set(manifest["provenance"]["notices"]) | {"manifest.json"}
        self.assertEqual({name.removeprefix(prefix) for name in self.archive.namelist() if name.startswith(prefix)}, expected)

    def test_wheel_version_matches_project_and_lock(self) -> None:
        from packaging.version import Version
        root = Path(__file__).resolve().parents[1]
        project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
        lock = tomllib.loads((root / "uv.lock").read_text())
        metadata = BytesParser().parsebytes(self.archive.read(next(n for n in self.archive.namelist() if n.endswith(".dist-info/METADATA"))))
        self.assertGreater(Version(project["version"]), Version("0.1.0"))
        self.assertEqual(metadata["Version"], project["version"])
        self.assertEqual(next(p for p in lock["package"] if p["name"] == project["name"])["version"], project["version"])
        self.assertEqual(Path(self.archive.filename).name, f"checksi-{project['version']}-py3-none-any.whl")

    def test_installed_checker_authenticates_model_resources_and_license_copies(self) -> None:
        path = Path(__file__).resolve().parents[1] / "scripts/dp5q_packaging_check.py"
        spec = importlib.util.spec_from_file_location("packaging_archive_check", path)
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        self.assertEqual(set(checker.validate_wheel_assets(Path(self.archive.filename))),
                         {"weights.npz", "LICENSE.dp5.txt", "LICENSE.cascade.txt", "LICENSE.nfp.txt", "LICENSE.nmrshiftdb2.txt", "NOTICE.txt"})
        names = self.archive.namelist()
        targets = ["checksi/dp5q/data/NOTICE.txt", next(name for name in names if name.endswith(
            ".dist-info/licenses/src/checksi/dp5q/data/NOTICE.txt"))]
        for target in targets:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                modified = Path(directory) / "modified.whl"
                with ZipFile(modified, "w") as output:
                    for name in names:
                        data = self.archive.read(name)
                        output.writestr(name, b"x" + data[1:] if name == target else data)
                with self.assertRaisesRegex(ValueError, "manifest|notice"):
                    checker.validate_wheel_assets(modified)

    def test_checkout_notices_match_manifest_and_hash_pinned_paths_disable_conversion(self) -> None:
        root = Path(__file__).resolve().parents[1]
        assets = root / "src/checksi/dp5q/data"
        metadata = json.loads((assets / "manifest.json").read_bytes())
        pins = metadata["provenance"]["notices"]
        self.assertEqual(set(pins), {path.name for path in assets.glob("*.txt")})
        for name, info in pins.items():
            data = (assets / name).read_bytes()
            self.assertEqual(len(data), info["bytes"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), info["sha256"])
        paths = [path.relative_to(root).as_posix() for path in assets.iterdir() if path.is_file()]
        paths.extend(path.relative_to(root).as_posix() for path in (root / "scripts/dp5q-reference/notices").iterdir())
        paths.extend(("scripts/dp5q-reference/source-lock.json", "scripts/dp5q-reference/tensor-map.json"))
        result = subprocess.run(["git", "-c", "core.autocrlf=true", "check-attr", "-z", "text", "--stdin"],
                                input=("\0".join(paths) + "\0").encode(), cwd=root, capture_output=True, check=True)
        expected = [field for path in paths for field in (path.encode(), b"text", b"unset")] + [b""]
        self.assertEqual(result.stdout.split(b"\0"), expected)

    def test_distribution_license_metadata_includes_all_bundled_terms(self) -> None:
        names = self.archive.namelist()
        metadata = BytesParser().parsebytes(self.archive.read(next(n for n in names if n.endswith(".dist-info/METADATA"))))
        self.assertEqual(metadata["License-Expression"], "MIT AND LicenseRef-NFP AND LicenseRef-NMRShiftDB2 AND OFL-1.1")
        root = Path(__file__).resolve().parents[1]
        expected = {"LICENSE", *[path.relative_to(root).as_posix() for path in
                    (root / "src/checksi/dp5q/data").glob("LICENSE*.txt")],
                    "src/checksi/dp5q/data/NOTICE.txt", "src/checksi/resources/fonts/LICENSE.txt"}
        self.assertEqual(set(metadata.get_all("License-File")), expected)
        for path in expected:
            entry = next(name for name in names if name.endswith(".dist-info/licenses/" + path))
            self.assertEqual(self.archive.read(entry), (root / path).read_bytes())

    def test_no_executable_reference_models_or_base_gui_dependencies(self) -> None:
        names = self.archive.namelist()
        self.assertNotIn("checksi/dp5_runtime.py", names)
        self.assertFalse(any(n.endswith((".hdf5", ".h5", ".keras", ".p", ".pkl", ".pickle", ".dp5")) for n in names))
        metadata = BytesParser().parsebytes(self.archive.read(next(n for n in names if n.endswith(".dist-info/METADATA"))))
        requirements = metadata.get_all("Requires-Dist")
        for requirement in requirements:
            lowered = requirement.lower()
            self.assertFalse(lowered.startswith("comtypes"), "Removed Word/COM support must not remain a runtime dependency")
            self.assertFalse(any(name in lowered for name in ("tensorflow", "keras", "nfp", "dp4", "dp5==")))
            if lowered.startswith(("pyside6", "qt-material")):
                self.assertIn('extra == "gui"', requirement)


class WheelhousePlanningTests(unittest.TestCase):
    """A preparation plan is bounded and contains only pinned public artifacts."""

    @classmethod
    def setUpClass(cls) -> None:
        path = Path(__file__).resolve().parents[1] / "scripts" / "dp5q_packaging_check.py"
        spec = importlib.util.spec_from_file_location("dp5q_packaging_check", path)
        cls.checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.checker)

    def artifact(self, name="demo-1.0-py3-none-any.whl", size=10):
        return {"url": "https://files.pythonhosted.org/packages/" + name, "size": size, "hash": "sha256:" + "0" * 64}

    def test_packaging_tool_is_explicitly_dev_only(self):
        project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
        self.assertIn("packaging==26.0", project["dependency-groups"]["dev"])
        self.assertFalse(any(requirement.lower().startswith("packaging")
                             for requirement in project["project"]["dependencies"]))

    def test_build_staging_excludes_untracked_source_but_keeps_tracked_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "repository", root / "stage"
            source.mkdir()
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            package = source / "src/checksi"
            package.mkdir(parents=True)
            for name in ("pyproject.toml", "README.md", "LICENSE", "src/checksi/__init__.py"):
                (source / name).write_text("tracked", encoding="utf-8")
            subprocess.run(["git", "add", "--", "pyproject.toml", "README.md", "LICENSE", "src"], cwd=source, check=True)
            (package / "__init__.py").write_text("tracked edit", encoding="utf-8")
            (package / "private_scratch.py").write_text("not a release input", encoding="utf-8")
            self.checker.stage_source(source, destination)
            self.assertEqual((destination / "src/checksi/__init__.py").read_text(encoding="utf-8"), "tracked edit")
            self.assertFalse((destination / "src/checksi/private_scratch.py").exists())

    def test_doctor_and_run_guard_activations_require_safe_path(self):
        for role, arguments in (("doctor", ["--self-check"]),
                                ("run", ["--run-id", "run", "--generation", "1"])):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                log = Path(directory) / "startups.jsonl"
                state = {"pid": 10, "ppid": 1, "file": "guard.py", "sha256": "a" * 64,
                         "network_self_check": True, "flags": {"safe_path": False},
                         "orig_argv": ["python", "-m", "checksi.dp5q.worker", *arguments]}
                manifest = {"guard": {"path": "guard.py", "sha256": "a" * 64}, "startup_log": str(log)}
                log.write_text(json.dumps(state) + "\n")
                with self.assertRaisesRegex(ValueError, "safe_path"):
                    self.checker.read_guard_startups(manifest)
                state["flags"]["safe_path"] = True
                log.write_text(json.dumps(state) + "\n")
                self.assertEqual(self.checker.read_guard_startups(manifest), [state])

    def test_cwd_probes_detect_unsafe_imports_and_allow_safe_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            probes = self.checker.plant_import_probes(root)
            environment = {key: value for key, value in os.environ.items()
                           if key not in {"PYTHONPATH", "PYTHONSAFEPATH"}}
            unsafe = subprocess.run([sys.executable, "-c", "import queue"], cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(unsafe.returncode, 0)
            marker = Path(probes["queue"]["marker"])
            self.assertTrue(marker.is_file())
            marker.unlink()
            safe = subprocess.run([sys.executable, "-P", "-c", "import queue,json,numpy"], cwd=root,
                                  env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(safe.returncode, 0, safe.stdout + safe.stderr)
            self.assertTrue(all(not Path(probe["marker"]).exists() for probe in probes.values()))

    def test_selects_compatible_locked_wheel_and_source_only_fallback(self):
        lock = {"package": [
            {"name": "demo", "version": "1.0", "wheels": [self.artifact()]},
            {"name": "source", "version": "2.0", "sdist": self.artifact("source-2.0.tar.gz", 20)},
        ]}
        plan = self.checker.plan_artifacts(lock, {"demo": "1.0", "source": "2.0"}, ["py3-none-any"], 30)
        self.assertEqual([item["name"] for item in plan], ["demo", "source"])
        self.assertEqual(sum(item["size"] for item in plan), 30)

    def test_rejects_budget_or_private_source_before_download(self):
        lock = {"package": [{"name": "demo", "version": "1.0", "wheels": [self.artifact()]}]}
        with self.assertRaisesRegex(ValueError, "budget"):
            self.checker.plan_artifacts(lock, {"demo": "1.0"}, ["py3-none-any"], 9)
        lock["package"][0]["wheels"][0]["url"] = "https://private.example/demo-1.0-py3-none-any.whl"
        with self.assertRaisesRegex(ValueError, "public"):
            self.checker.plan_artifacts(lock, {"demo": "1.0"}, ["py3-none-any"], 100)

    def test_rejects_dependency_missing_from_lock_or_locked_at_another_version(self):
        lock = {"package": [{"name": "demo", "version": "1.0", "wheels": [self.artifact()]}]}
        for active in ({"demo": "1.0", "missing": "2.0"}, {"demo": "2.0"}):
            with self.subTest(active=active), self.assertRaisesRegex(ValueError, "closure"):
                self.checker.plan_artifacts(lock, active, ["py3-none-any"], 100)

    def test_prepare_refuses_short_corrupt_and_oversized_downloads_offline(self):
        payload = b"safe wheel bytes"
        artifact = {"name": "demo", "version": "1.0", **self.artifact(size=len(payload)),
                    "hash": "sha256:" + hashlib.sha256(payload).hexdigest()}
        for body in (payload[:-1], b"x" * len(payload), payload + b"unlocked trailing bytes"):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "wheelhouse"
                with patch.object(self.checker, "plan_artifacts", return_value=[artifact]), \
                     patch.object(self.checker, "_base_closure", return_value={}), \
                     patch.object(self.checker, "urlopen", return_value=io.BytesIO(body)), \
                     self.assertRaisesRegex(ValueError, "hash/size|locked size|budget"):
                    self.checker.prepare(output, len(payload) + 100)
                self.assertFalse((output / "closure.json").exists())

    def test_prepare_accounts_failed_attempt_bytes_against_total_budget(self):
        class Interrupted(io.BytesIO):
            def read(self, size=-1):
                if self.tell():
                    raise TimeoutError("controlled interrupted public download")
                return super().read(min(size, 4))
        payload = b"safe wheel bytes"
        artifact = {"name": "demo", "version": "1.0", **self.artifact(size=len(payload)),
                    "hash": "sha256:" + hashlib.sha256(payload).hexdigest()}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(self.checker, "plan_artifacts", return_value=[artifact]), \
                 patch.object(self.checker, "_base_closure", return_value={}), \
                 patch.object(self.checker, "urlopen", side_effect=[Interrupted(payload), io.BytesIO(payload)]), \
                 patch.object(self.checker.time, "sleep"), self.assertRaisesRegex(ValueError, "budget"):
                self.checker.prepare(Path(directory) / "wheelhouse", len(payload))

    def test_verify_refuses_modified_wheelhouse_before_creating_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheelhouse = root / "wheelhouse"
            wheelhouse.mkdir()
            dependency = wheelhouse / "demo-1.0-py3-none-any.whl"
            dependency.write_bytes(b"original wheel")
            receipt = {"wheels": self.checker._file_receipts([dependency])}
            (wheelhouse / "closure.json").write_text(json.dumps(receipt))
            dependency.write_bytes(b"modified wheel")
            with patch.object(self.checker.subprocess, "run") as run, \
                 self.assertRaisesRegex(ValueError, "wheelhouse"):
                self.checker.verify(root / "unused.whl", wheelhouse)
            run.assert_not_called()

    def test_wheelhouse_authenticates_exact_locked_artifacts_and_wheel_inventory(self):
        payload = b"safe wheel bytes"
        artifact = {"name": "demo", "version": "1.0", **self.artifact(size=len(payload)),
                    "hash": "sha256:" + hashlib.sha256(payload).hexdigest()}
        for mutation in ("artifact-hash", "missing-closure", "extra-wheel", "receipted-extra-wheel",
                         "receipted-newer-wheel", "transferred-budget"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                dependency = root / "demo-1.0-py3-none-any.whl"
                dependency.write_bytes(payload)
                receipt = {"wheels": self.checker._file_receipts([dependency]), "artifacts": [artifact.copy()],
                           "max_download_bytes": 100, "download_bytes": len(payload), "transferred_bytes": len(payload)}
                with patch.object(self.checker, "plan_artifacts", return_value=[artifact]), \
                     patch.object(self.checker, "_base_closure", return_value={}):
                    (root / "closure.json").write_text(json.dumps(receipt))
                    self.checker.validate_wheelhouse(root)
                    if mutation == "artifact-hash":
                        dependency.write_bytes(b"x" * len(payload))
                        receipt["wheels"] = self.checker._file_receipts([dependency])
                    elif mutation == "missing-closure":
                        receipt["artifacts"] = []
                    elif mutation == "extra-wheel":
                        (root / "extra-1.0-py3-none-any.whl").write_bytes(payload)
                    elif mutation in ("receipted-extra-wheel", "receipted-newer-wheel"):
                        name = "extra-1.0" if mutation == "receipted-extra-wheel" else "demo-9.0"
                        (root / (name + "-py3-none-any.whl")).write_bytes(payload)
                        receipt["wheels"] = self.checker._file_receipts(root.glob("*.whl"))
                    else:
                        receipt["transferred_bytes"] = 101
                    (root / "closure.json").write_text(json.dumps(receipt))
                    with self.assertRaisesRegex(ValueError, "wheelhouse"):
                        self.checker.validate_wheelhouse(root)

    def test_installed_probe_guard_blocks_network_and_reference_imports(self):
        checks = '''
import socket
for operation in (lambda: socket.getaddrinfo("127.0.0.1", 443),
                  lambda: socket.socket().connect(("127.0.0.1", 9)),
                  lambda: socket.socket().bind(("127.0.0.1", 0)),
                  lambda: __import__("tensorflow")):
    try:
        operation()
    except RuntimeError:
        pass
    else:
        raise AssertionError("Installed verification guard did not block the operation")
'''
        result = subprocess.run([sys.executable, "-I", "-c", self.checker.NETWORK_GUARD + checks],
                                capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_installed_versions_must_match_exact_locked_closure(self):
        expected = {"checksi": "0.2.0", "demo": "1.0"}
        self.checker.validate_installed_versions(expected, expected)
        for actual in ({"checksi": "0.2.0"}, {**expected, "extra": "1.0"},
                       {**expected, "demo": "9.0"}):
            with self.subTest(actual=actual), self.assertRaisesRegex(ValueError, "installed distribution"):
                self.checker.validate_installed_versions(actual, expected)


class InstalledGuardStartupTests(unittest.TestCase):
    """Real isolated interpreters must guard startup despite a base-module shadow."""

    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "scripts" / "dp5q_packaging_check.py"
        spec = importlib.util.spec_from_file_location("packaging_startup_check", path)
        self.checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.checker)
        directory = tempfile.TemporaryDirectory(prefix="dp5q-guard-startup-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        environment = self.root / "environment"
        venv.EnvBuilder(with_pip=False).create(environment)
        self.python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        self.site = Path(subprocess.check_output([str(self.python), "-I", "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True, timeout=10).strip())
        self.log = self.root / "guard-startups.jsonl"
        shadow = self.root / "base-shadow"
        shadow.mkdir()
        self.shadow_file = shadow / "sitecustomize.py"
        self.shadow_file.write_text("SHADOW_SOURCE = __file__\n")
        (self.site / "000-base-shadow.pth").write_text(
            "import sys; sys.path.insert(0, " + repr(str(shadow)) + ")\n")

    def execute(self, code):
        return subprocess.run([str(self.python), "-I", "-c", code], cwd=self.root,
                              capture_output=True, text=True, timeout=10, check=False)

    def test_absolute_startup_guard_survives_shadow_in_parent_and_child(self):
        manifest = self.checker.install_verification_guard(self.site, self.log)
        checks = '''import json, socket, sys, sitecustomize
assert sys._dp5q_verification_guard["network_self_check"]
assert sys.flags.isolated
try:
    socket.getaddrinfo("127.0.0.1", 0, flags=socket.AI_NUMERICHOST)
except RuntimeError as exc:
    assert "Network disabled" in str(exc)
else:
    raise AssertionError("Network guard inactive")
print(json.dumps({"guard":sys._dp5q_verification_guard,"shadow":sitecustomize.__file__}))
'''
        code = checks + "\nimport subprocess\nsubprocess.run([sys.executable, '-I', '-c', " + repr(checks) + "], check=True)\n"
        result = self.execute(code)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(Path(row["shadow"]), self.shadow_file)
            self.assertEqual(row["guard"]["file"], manifest["guard"]["path"])
            self.assertEqual(row["guard"]["sha256"], manifest["guard"]["sha256"])
            self.assertGreater(row["guard"]["ppid"], 0)
            self.assertLessEqual(len(row["guard"]["orig_argv"]), 32)
        starts = self.checker.read_guard_startups(manifest)
        self.assertEqual(len(starts), 2)
        self.assertEqual(len({row["pid"] for row in starts}), 2)

    def test_missing_guard_file_exits_before_application_code(self):
        manifest = self.checker.install_verification_guard(self.site, self.log)
        Path(manifest["guard"]["path"]).unlink()
        result = self.execute("raise AssertionError('application code must not run')")
        self.assertEqual(result.returncode, 86, result.stdout + result.stderr)
        self.assertIn("verification guard bootstrap failed", result.stderr)
        self.assertFalse(self.log.exists())

    def test_modified_guard_or_bootstrap_bytes_refuse_probe_before_import(self):
        for name in ("guard", "bootstrap"):
            with self.subTest(name=name):
                manifest = self.checker.install_verification_guard(self.site, self.log)
                path = Path(manifest[name]["path"])
                data = path.read_bytes()
                # Keep executable behavior intact and update size; only the hash detects this.
                path.write_bytes(data + b"# altered after receipt\n")
                manifest[name]["bytes"] = path.stat().st_size
                manifest_path = self.root / "guard-manifest.json"
                manifest_path.write_text(json.dumps(manifest))
                script = Path(self.checker.__file__)
                result = self.execute("import runpy; checker=runpy.run_path(" + repr(str(script)) + "); "
                    "checker['_require_guard'](" + repr(str(manifest_path)) + ")")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Verification startup guard", result.stderr)

    def test_startup_receipt_hash_mutation_is_refused(self):
        manifest = self.checker.install_verification_guard(self.site, self.log)
        result = self.execute("pass")
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads(self.log.read_text())
        state["sha256"] = "0" * 64
        self.log.write_text(json.dumps(state) + "\n")
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.checker.read_guard_startups(manifest)

    def test_missing_bootstrap_refuses_probe_before_application_import(self):
        manifest = self.checker.install_verification_guard(self.site, self.log)
        Path(manifest["bootstrap"]["path"]).unlink()
        manifest_path = self.root / "guard-manifest.json"
        manifest_path.write_text(json.dumps(manifest))
        script = Path(self.checker.__file__)
        result = self.execute("import runpy; checker=runpy.run_path(" + repr(str(script)) + "); "
            "checker['probe'](" + repr(str(manifest_path)) + ")")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Verification startup guard", result.stderr)
        self.assertNotIn("No module named 'checksi'", result.stderr)


class GuardProcessIdentityTests(unittest.TestCase):
    """A launcher relationship never replaces the exact worker protocol identity."""

    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "scripts" / "dp5q_packaging_check.py"
        spec = importlib.util.spec_from_file_location("packaging_process_check", path)
        cls.checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.checker)

    def row(self, pid=100, ppid=1, run_id="owned-run", generation="1", module="checksi.dp5q.worker"):
        return {"pid": pid, "ppid": ppid, "orig_argv": ["python", "-u", "-m", module,
                "--run-id", run_id, "--generation", generation]}

    def test_direct_and_redirected_interpreters_require_exact_worker_tokens(self):
        for row in (self.row(), self.row(pid=101, ppid=100)):
            with self.subTest(row=row):
                matched = self.checker.match_worker_startup([row], 100, "owned-run", 1)
                self.assertEqual(matched, row)

    def test_wrong_module_tokens_or_lineage_fail_with_bounded_diagnostics(self):
        for row in (self.row(run_id="other"), self.row(generation="2"), self.row(module="other.worker"),
                    self.row(pid=101, ppid=999)):
            with self.subTest(row=row), self.assertRaisesRegex(RuntimeError, "Worker startup identity mismatch") as error:
                self.checker.match_worker_startup([row], 100, "owned-run", 1)
            self.assertIn('"launcher_pid": 100', str(error.exception))
            self.assertIn('"orig_argv"', str(error.exception))

    def test_ambiguous_interpreters_and_duplicate_tokens_are_refused(self):
        cases = [[self.row(), self.row(pid=101, ppid=100)],
                 [self.row() | {"orig_argv": self.row()["orig_argv"] + ["--run-id", "owned-run"]}]]
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(RuntimeError):
                self.checker.match_worker_startup(rows, 100, "owned-run", 1)

    def test_actual_interpreter_observation_is_read_only_and_detects_exit(self):
        with tempfile.TemporaryDirectory(prefix="dp5q-observation-proof-") as directory:
            identity = Path(directory) / "pid"
            code = ("import os,sys; from pathlib import Path; p=Path(sys.argv[1]); "
                    "p.with_suffix('.tmp').write_text(str(os.getpid())); "
                    "p.with_suffix('.tmp').replace(p); sys.stdin.buffer.read(1)")
            child = subprocess.Popen([sys.executable, "-I", "-c", code, str(identity)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not identity.exists():
                    if child.poll() is not None or time.monotonic() >= deadline:
                        self.fail("Interpreter did not provide its bounded identity handshake")
                    time.sleep(.02)
                actual_pid = int(identity.read_text())
                with self.checker.NativeProcessObservation(actual_pid) as observation:
                    self.assertTrue(observation.is_running())
                    child.stdin.write(b"x")
                    child.stdin.flush()
                    child.wait(timeout=5)
                    self.assertTrue(observation.wait_gone(timeout=2)["gone"])
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)
                for stream in (child.stdin, child.stdout, child.stderr):
                    stream.close()

    @unittest.skipUnless(sys.platform == "win32", "Requires the native Windows venv redirector")
    def test_forced_launcher_termination_signals_actual_interpreter_before_stop_file(self):
        with tempfile.TemporaryDirectory(prefix="dp5q-redirector-proof-") as directory:
            root = Path(directory)
            handshake, stop = root / "identity.json", root / "stop"
            nonce = uuid.uuid4().hex
            venv.EnvBuilder(with_pip=False).create(root / "environment")
            launcher = root / "environment" / "Scripts" / "python.exe"
            code = '''import json,os,sys,time
from pathlib import Path
identity,stop,nonce=sys.argv[1:]
with open(identity+".tmp","x") as output:
    json.dump({"pid":os.getpid(),"ppid":os.getppid(),"nonce":nonce},output)
os.replace(identity+".tmp",identity)
deadline=time.monotonic()+60
while not Path(stop).exists() and time.monotonic()<deadline:time.sleep(.02)
'''
            child = subprocess.Popen([str(launcher), "-I", "-c", code, str(handshake), str(stop), nonce],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            try:
                deadline = time.monotonic() + 5
                while not handshake.exists():
                    if child.poll() is not None or time.monotonic() >= deadline:
                        self.fail("Owned redirector fixture did not provide its bounded handshake")
                    time.sleep(.02)
                identity = json.loads(handshake.read_text())
                self.assertEqual(identity["nonce"], nonce)
                self.assertEqual(identity["ppid"], child.pid)
                self.assertNotEqual(identity["pid"], child.pid, "This proof requires an actual venv redirector")
                with self.checker.NativeProcessObservation(identity["pid"]) as observation:
                    try:
                        self.assertFalse(stop.exists())
                        child.terminate()  # Only the owned launcher; never signal the discovered PID.
                        proof = observation.wait_gone(timeout=2)
                        self.assertFalse(stop.exists(), "Cooperative cleanup must not produce a false pass")
                        print("WINDOWS_REDIRECTOR_KILL_ON_CLOSE_PROOF " + json.dumps({
                            "launcher_pid": child.pid, "identity": identity, "exit": proof,
                            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}), flush=True)
                    finally:
                        stop.touch()
                        if observation.is_running():
                            observation.wait_gone(timeout=3)
            finally:
                stop.touch()
                if child.poll() is None:
                    try:
                        child.wait(timeout=3)  # Let the owned fixture observe its stop file first.
                    except subprocess.TimeoutExpired:
                        child.terminate()
                child.wait(timeout=3)
                child.stdout.close()
                child.stderr.close()
