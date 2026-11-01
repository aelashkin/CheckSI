#!/usr/bin/env python3
"""Build distributions and verify real installed CLI/GUI flows outside the checkout."""

from __future__ import annotations

import argparse
import copy
import configparser
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from zipfile import ZipFile


RESOURCES = ("defaults.toml", "gui/assets/checksi_logo.png",
             "resources/fonts/NotoSans-Regular.ttf", "resources/fonts/NotoSans-Bold.ttf",
             "resources/fonts/LICENSE.txt", "resources/fonts/SOURCE.txt")
FIXTURE = ("1H NMR (400 MHz, CDCl3): δ 3.50 (s, 2H), 1.20 (s, 3H). "
           "13C NMR (100 MHz, CDCl3): δ 60.0, 20.0. "
           "HRMS (ESI): calcd for C2H7O+ [M+H]+ 47.0491, found 47.0501. "
           "Experimental characterisation. "
           "1H NMR (400 MHz, CDCl3): δ 3.60 (s, 2H), 1.30 (s, 3H). "
           "13C NMR (100 MHz, CDCl3): δ 61.0, 21.0. "
           "Anal. Calcd for C2H6O: C, 52.14; H, 13.13. Found: C, 51.73; H, 13.13.")


def digest(path: Path) -> str:
    """Hash one bounded local verification artifact."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def semantic_projection(payload: dict) -> dict:
    """Keep all analytical values verbatim; source/export/provenance vary by run."""
    return copy.deepcopy({key: payload[key] for key in ("schema_version", "analysis", "finding_severities")})


def annotation_projection(associations: list[dict]) -> list[tuple]:
    """Compare source-to-compound mappings while allowing page/rectangle layout variance."""
    return sorted({(item["compound_id"], item["phrase"], item["color"]) for item in associations})


def validate_findings_pdf(text: str, fonts: list[str]) -> None:
    """Require scientific text and the actual bundled font family names in the PDF."""
    expected = {"Noto Sans Regular", "Noto Sans Bold"}
    if not expected.issubset({name.rsplit("+", 1)[-1] for name in fonts}):
        raise RuntimeError(f"Findings PDF lost its bundled font families: {fonts}")
    if "δ" not in text or "1H NMR" not in text or "�" in text:
        raise RuntimeError(f"Findings PDF lost scientific Unicode: {text[:500]!r}")


def validate_resources(wheel: Path, sdist: Path, source: Path) -> dict:
    """Check exact source resource bytes in both distributable formats."""
    receipts = {}
    with ZipFile(wheel) as binary, tarfile.open(sdist, "r:gz") as archive:
        for name in RESOURCES:
            expected = (source / name).read_bytes()
            wheel_name = "checksi/" + name
            matches = [item for item in archive.getmembers() if item.name.endswith("/src/" + wheel_name)]
            if not expected or binary.namelist().count(wheel_name) != 1 or len(matches) != 1 or not matches[0].isfile():
                raise ValueError(f"Missing or duplicate distribution resource: {name}")
            member = archive.extractfile(matches[0])
            if binary.read(wheel_name) != expected or member.read(len(expected) + 1) != expected:
                raise ValueError(f"Distribution resource differs from source: {name}")
            receipts[name] = {"bytes": len(expected), "sha256": hashlib.sha256(expected).hexdigest()}
    return receipts


def validate_wheel_contract(wheel: Path, source: Path) -> dict:
    """Verify packaged runtime resources and both public installed entry points."""
    resources = {}
    with ZipFile(wheel) as archive:
        names = archive.namelist()
        for name in RESOURCES:
            target = "checksi/" + name
            expected = (source / name).read_bytes()
            if not expected or names.count(target) != 1 or archive.read(target) != expected:
                raise ValueError(f"Missing or changed wheel resource: {name}")
            resources[name] = {"bytes": len(expected), "sha256": hashlib.sha256(expected).hexdigest()}
        entrypoints = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        if len(entrypoints) != 1:
            raise ValueError("The wheel is missing unique console entrypoint metadata.")
        metadata = configparser.ConfigParser(interpolation=None)
        metadata.optionxform = str
        metadata.read_string(archive.read(entrypoints[0]).decode("utf-8"))
        expected_scripts = {"checksi": "checksi.main:entrypoint", "checksi-gui": "checksi.gui_main:main_gui"}
        if not metadata.has_section("console_scripts") or dict(metadata["console_scripts"]) != expected_scripts:
            raise ValueError("The wheel console entrypoint contract differs.")
    return {"resources": resources, "console_scripts": expected_scripts}


def packaging_helper():
    """Reuse the bounded, hash-verified native wheelhouse implementation."""
    spec = importlib.util.spec_from_file_location("native_packaging", Path(__file__).with_name("dp5q_packaging_check.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def locked_constraints(source: Path) -> str:
    """Export portable runtime constraints, including optional GUI pins and conditional markers."""
    return subprocess.run(["uv", "export", "--locked", "--offline", "--no-cache", "--all-extras", "--no-default-groups",
                           "--no-dev", "--no-emit-project", "--no-hashes", "--no-header", "--no-annotate",
                           "--python", sys.executable], cwd=source, check=True, capture_output=True, text=True, encoding="utf-8", timeout=30).stdout


def build_distributions(destination: Path) -> dict:
    """Build a clean sdist, then its wheel, without old checkout build artifacts."""
    source = Path(__file__).resolve().parents[1]
    destination.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="portability-build-") as directory:
        stage = Path(directory)
        packaging_helper().stage_source(source, stage)
        subprocess.run(["uv", "build", "--offline", "--no-build-isolation", "--python", sys.executable,
                        "--out-dir", str(destination)], cwd=stage, check=True, capture_output=True, text=True, encoding="utf-8", timeout=120)
    wheels, sources = list(destination.glob("*.whl")), list(destination.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise ValueError("Expected exactly one wheel and one source distribution.")
    constraints = destination / "constraints.txt"
    constraints.write_text(locked_constraints(source), encoding="utf-8")
    return {"wheel": str(wheels[0]), "sdist": str(sources[0]),
            "constraints": str(constraints), "constraints_sha256": digest(constraints), "constraints_bytes": constraints.stat().st_size,
            "resources": validate_resources(wheels[0], sources[0], source / "src/checksi"),
            "console_scripts": validate_wheel_contract(wheels[0], source / "src/checksi")["console_scripts"]}


def inventory(root: Path) -> dict:
    """Record every file so read-only verification also detects unexpected writes."""
    return {path.relative_to(root).as_posix(): digest(path) for path in sorted(root.rglob("*")) if path.is_file()}


def freeze(root: Path) -> list[tuple[Path, int]]:
    """Remove write bits only from this invocation's installed package or fixtures."""
    modes = [(path, stat.S_IMODE(path.stat().st_mode)) for path in [*root.rglob("*"), root] if not path.is_symlink()]
    for path, mode in modes:
        path.chmod(mode & ~0o222)
    return modes


def run(command: list[str], *, cwd: Path, expected: tuple[int, ...] = (0,), env=None, timeout=120) -> subprocess.CompletedProcess:
    """Bound each actual child command and retain useful failures."""
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=timeout, check=False)
    if result.returncode not in expected:
        raise RuntimeError(f"{command!r} exited {result.returncode}:\n{result.stdout}\n{result.stderr}")
    return result


def isolated_python(python: Path, *arguments: str) -> list[str]:
    """Use UTF-8 for the owned isolated protocol without changing child CLI locale."""
    return [str(python), "-I", "-X", "utf8", *arguments]


def require_converter(executable: str | None, *, required: bool) -> None:
    """Reject a missing required converter instead of testing only the partial path."""
    if required and (not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK)):
        raise ValueError("The required LibreOffice executable was not provisioned or is not executable.")


def validate_native_converters(receipts: list[dict]) -> None:
    """Require real DOCX conversion receipts from every Linux/macOS Python 3.13 lane."""
    for receipt in receipts:
        if receipt["python"].startswith("3.13.") and receipt["platform"].startswith(("Linux", "macOS")):
            complete = any(case["case"] == "docx" and not case["partial"] for case in receipt["cases"])
            if not receipt.get("required_libreoffice") or not receipt.get("libreoffice") or not complete:
                raise ValueError("A native lane lacks its required LibreOffice conversion evidence.")


def validate_native_targets(receipts: list[dict]) -> None:
    """Require every Linux/Windows x86-64 Python lane before comparing CI receipts."""
    expected = {(system, python) for system in ("Linux", "Windows") for python in ("3.12", "3.13")}
    observed = {
        (receipt["platform"].split("-")[0], ".".join(receipt["python"].split(".")[:2]))
        for receipt in receipts if receipt["machine"].lower() in ("x86_64", "amd64")
    }
    if not expected.issubset(observed):
        raise RuntimeError("Native comparison needs all Linux/Windows Python 3.12/3.13 x86-64 lanes.")


def gui_probe(package: Path, console: Path, cwd: Path) -> dict:
    """Launch the real installed GUI entry function and close from the Qt thread."""
    from PySide6.QtCore import QMetaObject, QObject, Qt, QTimer, Slot, qVersion
    from PySide6.QtWidgets import QApplication, QMainWindow
    from checksi.gui.main_window import APP_ICON_PATH
    run([str(console), "--help"], cwd=cwd)
    state = {}

    class Observer(QObject):
        @Slot()
        def observe(self):
            windows = [window for window in QApplication.topLevelWidgets() if isinstance(window, QMainWindow)]
            if not windows:
                QTimer.singleShot(50, self.observe)
                return
            state.update(windows=len(windows), icon=not windows[0].windowIcon().isNull(),
                         title=windows[0].windowTitle(), qt=qVersion(), icon_sha256=digest(APP_ICON_PATH))
            if not windows[0].grab().save(str(cwd / "installed-gui.png")):
                raise RuntimeError("The installed GUI screenshot could not be saved.")
            QTimer.singleShot(100, windows[0].close)

    observer = Observer()
    def await_application():
        for _ in range(1000):
            if QApplication.instance() is not None:
                QMetaObject.invokeMethod(observer, "observe", Qt.ConnectionType.QueuedConnection)
                return
            time.sleep(.01)
    watcher = threading.Thread(target=await_application, daemon=True)
    watcher.start()
    entry = next(item for item in importlib.metadata.entry_points(group="console_scripts") if item.name == "checksi-gui")
    code = entry.load()(["--config-file", str(package / "defaults.toml")])
    watcher.join(timeout=1)
    if code != 0 or not state.get("icon"):
        raise RuntimeError(f"The installed GUI did not launch with its packaged icon: {state}")
    return state


def probe(workspace: Path, *, gui: bool, libreoffice: str | None, require_libreoffice: bool = False) -> dict:
    """Exercise installed artifacts with real input extraction, chemistry and exporters."""
    import checksi
    require_converter(libreoffice, required=require_libreoffice)
    package = Path(checksi.__file__).resolve().parent
    if Path(sys.prefix).resolve() not in package.parents or not getattr(sys, "_dp5q_verification_guard", {}).get("network_self_check"):
        raise RuntimeError("Probe must use the guarded, isolated installed environment.")
    versions = {item.metadata["Name"].lower().replace("_", "-"): item.version for item in importlib.metadata.distributions()}
    scripts = Path(sys.executable).parent
    console = scripts / ("checksi-gui.exe" if gui and os.name == "nt" else "checksi-gui" if gui else "checksi.exe" if os.name == "nt" else "checksi")
    before = inventory(package)
    modes = freeze(package)
    try:
        receipt = {"kind": "gui" if gui else "base", "python": sys.version, "platform": platform.platform(),
                   "required_libreoffice": require_libreoffice,
                   "machine": platform.machine(), "installed_package": str(package), "versions": versions,
                   "resources": {name: digest(package / name) for name in RESOURCES},
                   "read_only_enforcement": "POSIX write bits removed" if os.name == "posix" else "Windows read-only file attributes; inventory compared"}
        if gui:
            receipt["gui"] = gui_probe(package, console, workspace)
        else:
            if any(name.startswith(("pyside6", "shiboken6", "qt-material")) for name in versions):
                raise RuntimeError("The base environment contains optional Qt dependencies.")
            import fitz
            from docx import Document
            from docx.shared import Pt
            source = workspace / "read only inputs café α"
            source.mkdir()
            document = Document()
            document.styles["Normal"].font.name = "Arial"
            document.styles["Normal"].font.size = Pt(11)
            document.add_paragraph(FIXTURE)
            docx_path = source / "same chemistry α.docx"
            document.save(docx_path)
            pdf_path = source / "same chemistry α.pdf"
            with fitz.open() as pdf:
                page = pdf.new_page()
                page.insert_font(fontname="Fixture", fontfile=str(package / RESOURCES[2]))
                if page.insert_textbox(fitz.Rect(72, 72, 520, 720), FIXTURE, fontname="Fixture", fontsize=11) < 0:
                    raise RuntimeError("The generated PDF fixture did not fit.")
                pdf.save(pdf_path)
            inputs = inventory(source)
            modes.extend(freeze(source))
            from checksi.config import user_config_path
            settings_path = user_config_path()
            home = Path(os.environ["USERPROFILE"] if os.name == "nt" else os.environ["HOME"])
            home_before = sorted(path.relative_to(home).as_posix() for path in home.rglob("*"))
            factory = run([str(console), "config", "view"], cwd=workspace)
            if (settings_path.exists() or settings_path.parent.exists() or
                    sorted(path.relative_to(home).as_posix() for path in home.rglob("*")) != home_before):
                raise RuntimeError("Fresh default config view wrote a user configuration or directory.")
            receipt["factory_config"] = {"settings_path": str(settings_path), "created_files_or_directories": False,
                                         "configuration": json.loads(factory.stdout)}
            cfg = str(package / "defaults.toml")
            run([str(console), "--config-file", cfg, "config", "view"], cwd=workspace)
            cases = []
            for label, path, converter in (("pdf", pdf_path, libreoffice), ("docx", docx_path, libreoffice),
                                            ("docx-without-converter", docx_path, str(workspace / "missing converter"))):
                output = workspace / ("outputs " + label)
                command = [str(console), "--config-file", cfg, "run", str(path), "--output-dir", str(output),
                           "--override", "modes.deep_name_analysis=off", "--override", "modes.open_reports_on_success=false",
                           "--override", "dp5.enabled=false"]
                command.extend(["--override", "conversion.libreoffice_path=" + json.dumps(converter or str(workspace / "missing converter"), ensure_ascii=False)])
                completed = run(command, cwd=workspace, expected=(2, 3))
                reports = list(output.glob("*/results.json"))
                if len(reports) != 1:
                    raise RuntimeError("CLI did not produce one unique run-owned semantic report.")
                payload = json.loads(reports[0].read_text(encoding="utf-8"))
                analysis, export = payload["analysis"], payload["export"]
                partial = label == "docx-without-converter" or (label == "docx" and not libreoffice)
                if completed.returncode != (3 if partial else 2) or export["partial"] != partial or bool(export["artifact_warnings"]) != partial:
                    raise RuntimeError(f"Incorrect complete/partial result for {label}: {completed.stdout}")
                if analysis["total_found"] != 2 or analysis["error_stats"]["hrms_ppm_too_large"] != 2 or analysis["error_stats"]["ea_tolerance_exceeded"] != 1:
                    raise RuntimeError(f"The installed chemistry fixture was not actually analyzed: {label}: "
                                       f"{analysis['total_found']} compounds; {analysis['error_stats']}")
                files = {key: export[key] for key in ("html_path", "analysis_path", "pdf_path", "error_stats_path")}
                if not all(value and Path(value).is_file() for value in files.values()):
                    raise RuntimeError("Required native exporters did not produce their advertised files.")
                if bool(export["source_pdf_path"]) == partial:
                    raise RuntimeError("Source conversion/annotation metadata does not match its actual outcome.")
                owned = {row["compound_id"] for row in payload["source_associations"]}
                if not partial and owned != {compound["id"] for compound in analysis["compounds"]}:
                    raise RuntimeError("The simple native fixture lacks an owned source association for every compound.")
                with fitz.open(export["pdf_path"]) as pdf:
                    text = "".join(page.get_text() for page in pdf)
                    fonts = sorted({font[3] for page in pdf for font in page.get_fonts()})
                    validate_findings_pdf(text, fonts)
                cases.append({"case": label, "exit_code": completed.returncode, "partial": partial,
                              "semantics": semantic_projection(payload), "configuration": payload["configuration"],
                              "provenance": payload["provenance"], "pdf_fonts": fonts,
                              "source_associations": payload["source_associations"],
                              "annotation_ambiguities": payload.get("annotation_ambiguities", []),
                              "files": {key: {"bytes": Path(value).stat().st_size, "sha256": digest(Path(value))} for key, value in files.items()}})
            if cases[1]["semantics"] != cases[2]["semantics"]:
                raise RuntimeError("Converter availability changed analytical values or original text for the same DOCX.")
            if inventory(source) != inputs:
                raise RuntimeError("The application modified read-only source inputs.")
            receipt["cases"] = cases
            if libreoffice:
                version = run([libreoffice, "--version"], cwd=workspace, timeout=30)
                receipt["libreoffice"] = {"executable": libreoffice, "version": (version.stdout + version.stderr).strip()[:4096]}
        if inventory(package) != before:
            raise RuntimeError("The application modified its read-only installed package.")
        receipt["package_unchanged"] = True
        return receipt
    finally:
        for path, mode in reversed(modes):
            path.chmod(mode)


def verify(wheel: Path, sdist: Path, wheelhouse: Path, *, gui: bool, libreoffice: str | None,
           screenshot: Path | None = None, from_sdist: bool = False, constraints: Path | None = None,
           require_libreoffice: bool = False) -> dict:
    """Install only authenticated local wheels into a fresh non-editable environment."""
    helper = packaging_helper()
    require_converter(libreoffice, required=require_libreoffice)
    constraints = constraints or wheel.parent / "constraints.txt"
    if constraints.read_text(encoding="utf-8") != locked_constraints(Path(__file__).resolve().parents[1]):
        raise ValueError("Runtime constraints differ from the portable locked dependency export.")
    closure = helper.validate_wheelhouse(wheelhouse, extra="gui" if gui else "")
    with tempfile.TemporaryDirectory(prefix="portable installed café ") as directory:
        root = Path(directory)
        environment = root / "environment"
        home = root / "isolated user home"
        home.mkdir()
        env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSAFEPATH", "VIRTUAL_ENV"}}
        env.update(UV_OFFLINE="1", UV_NO_CACHE="1", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1", QT_QPA_PLATFORM="offscreen")
        env.update(HOME=str(home), USERPROFILE=str(home), XDG_CONFIG_HOME=str(home / "xdg"),
                   LOCALAPPDATA=str(home / "local"), APPDATA=str(home / "roaming"))
        run(["uv", "venv", "--python", sys.executable, str(environment)], cwd=root, env=env, timeout=30)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        installation = run(["uv", "pip", "install", "--python", str(python), "--offline", "--no-index", "--no-cache",
                            "--find-links", str(wheelhouse), "--constraint", str(constraints),
                            str(sdist if from_sdist else wheel) + ("[gui]" if gui else "")], cwd=root, env=env)
        site = run(isolated_python(python, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"), cwd=root, env=env).stdout.strip()
        helper.install_verification_guard(Path(site), root / "guard-startups.jsonl")
        script = root / "installed_probe.py"
        shutil.copy2(__file__, script)
        command = isolated_python(python, "-B", str(script), "probe", "--workspace", str(root)) + (["--gui"] if gui else [])
        if libreoffice:
            command += ["--libreoffice", libreoffice]
        if require_libreoffice:
            command += ["--require-libreoffice"]
        result = json.loads(run(command, cwd=root, env=env, timeout=300).stdout)
        if gui and screenshot:
            with screenshot.open("xb") as output:
                output.write((root / "installed-gui.png").read_bytes())
            result["gui"]["screenshot"] = {"name": screenshot.name, "sha256": digest(screenshot)}
        from packaging.utils import parse_wheel_filename
        helper.validate_installed_versions(result["versions"], {**helper._base_closure(extra="gui" if gui else ""),
                                                              "checksi": str(parse_wheel_filename(wheel.name)[1])})
        result.update(wheel={"name": wheel.name, "sha256": digest(wheel)}, sdist={"name": sdist.name, "sha256": digest(sdist)},
                      installed_from="sdist" if from_sdist else "wheel", installation_log=installation.stderr,
                      runtime_constraints={"name": constraints.name, "sha256": digest(constraints), "bytes": constraints.stat().st_size},
                      closure_receipt=closure, verification_script_sha256=digest(Path(__file__)))
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "prepare-gui", "verify", "probe", "compare"))
    for name in ("out-dir", "wheelhouse", "wheel", "sdist", "receipt", "workspace", "receipts", "constraints"):
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--gui", action="store_true")
    parser.add_argument("--from-sdist", action="store_true")
    parser.add_argument("--libreoffice")
    parser.add_argument("--require-libreoffice", action="store_true")
    args = parser.parse_args()
    if args.command == "build":
        result = build_distributions(args.out_dir.resolve())
    elif args.command == "prepare-gui":
        result = packaging_helper().prepare(args.wheelhouse.resolve(), 600 * 1024 * 1024, extra="gui")
    elif args.command == "probe":
        result = probe(args.workspace.resolve(), gui=args.gui, libreoffice=args.libreoffice,
                       require_libreoffice=args.require_libreoffice)
    elif args.command == "verify":
        result = verify(args.wheel.resolve(), args.sdist.resolve(), args.wheelhouse.resolve(), gui=args.gui,
                        libreoffice=args.libreoffice, screenshot=args.receipt.with_suffix(".png") if args.gui and args.receipt else None,
                        from_sdist=args.from_sdist, constraints=args.constraints.resolve() if args.constraints else None,
                        require_libreoffice=args.require_libreoffice)
    else:
        receipts = [item for path in args.receipts.rglob("portability-*.json")
                    if (item := json.loads(path.read_text(encoding="utf-8"))).get("kind") == "base"]
        validate_native_targets(receipts)
        validate_native_converters(receipts)
        expected = {case["case"]: case["semantics"] for case in receipts[0]["cases"] if case["case"] in ("pdf", "docx")}
        if any(case["semantics"] != expected["pdf" if case["case"] == "pdf" else "docx"]
               for item in receipts for case in item["cases"]):
            raise RuntimeError("Native targets differ in analytical values; inspect receipts before changing expectations.")
        source_mappings = {}
        for label in ("pdf", "docx"):
            mappings = [annotation_projection(case["source_associations"]) for item in receipts
                        for case in item["cases"] if case["case"] == label and not case["partial"]]
            if not mappings or not mappings[0] or any(mapping != mappings[0] for mapping in mappings):
                raise RuntimeError(f"Native {label} compound-to-phrase associations differ; inspect receipts.")
            source_mappings[label] = mappings[0]
        result = {"native_receipts": len(receipts), "semantics_equal": True, "semantics": expected,
                  "source_mappings": source_mappings,
                  "layout_note": "Source annotation rectangles, PDF layout, tool versions, input/export paths and file hashes are retained but not expected to match."}
    if args.receipt:
        with args.receipt.open("x", encoding="utf-8") as output:
            json.dump(result, output, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
