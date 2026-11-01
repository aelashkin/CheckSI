#!/usr/bin/env python3
"""Prepare a bounded public wheelhouse and verify a headless installed DP5q wheel."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from urllib.parse import urlsplit
from urllib.request import urlopen
from urllib.error import URLError
from zipfile import ZipFile


def plan_artifacts(lock: dict, active: dict[str, str], tags: list[str], budget: int) -> list[dict]:
    """Choose the exact installed native closure from uv.lock before downloading."""
    from packaging.utils import parse_wheel_filename
    ranks = {tag: rank for rank, tag in enumerate(tags)}
    plan = []
    for package in lock["package"]:
        if active.get(package["name"]) != package["version"]:
            continue
        choices = []
        for artifact in package.get("wheels", []):
            filename = Path(urlsplit(artifact["url"]).path).name
            compatible = [ranks[str(tag)] for tag in parse_wheel_filename(filename)[3] if str(tag) in ranks]
            if compatible:
                choices.append((min(compatible), artifact))
        artifact = min(choices, key=lambda choice: choice[0])[1] if choices else package.get("sdist")
        if artifact is None:
            raise ValueError(f"No locked native artifact for {package['name']}.")
        url = urlsplit(artifact["url"])
        if url.scheme != "https" or url.netloc != "files.pythonhosted.org":
            raise ValueError("Wheelhouse preparation permits only public PyPI artifacts.")
        plan.append({"name": package["name"], "version": package["version"], **artifact})
    if {item["name"] for item in plan} != set(active):
        raise ValueError("The installed dependency closure does not match uv.lock.")
    if sum(item["size"] for item in plan) > budget:
        raise ValueError("The locked artifact download exceeds the approved byte budget.")
    return plan


def _base_closure(*, extra: str = "") -> dict[str, str]:
    """Follow installed requirements, selecting only an explicitly requested root extra."""
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    pending, visited = ["checksi"], {}
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        distribution = importlib.metadata.distribution(name)
        visited[name] = distribution.version
        for raw in distribution.requires or []:
            requirement = Requirement(raw)
            if requirement.marker is None or requirement.marker.evaluate({"extra": extra if name == "checksi" else ""}):
                pending.append(canonicalize_name(requirement.name))
    del visited["checksi"]
    return visited


def prepare(wheelhouse: Path, max_bytes: int, *, extra: str = "") -> dict:
    """Download hash-verified locked artifacts, then build source-only wheels offline."""
    from packaging.tags import sys_tags
    lock = tomllib.loads((Path(__file__).resolve().parents[1] / "uv.lock").read_text())
    plan = plan_artifacts(lock, _base_closure(extra=extra), [str(tag) for tag in sys_tags()], max_bytes)
    wheelhouse.mkdir(parents=True, exist_ok=False)
    transferred = 0
    for artifact in plan:
        target = wheelhouse / Path(urlsplit(artifact["url"]).path).name
        for attempt in range(3):
            digest, received = hashlib.sha256(), 0
            try:
                with urlopen(artifact["url"], timeout=30) as response, target.open("xb") as output:
                    while True:
                        # One bounded look-ahead byte detects a correct locked prefix
                        # followed by unapproved bytes; a prefix hash alone is insufficient.
                        remaining = min(1024 * 1024, artifact["size"] - received + 1, max_bytes - transferred + 1)
                        if remaining <= 0:
                            raise ValueError("The public download exhausted its approved byte budget.")
                        block = response.read(remaining)
                        if not block:
                            break
                        received += len(block)
                        transferred += len(block)
                        if transferred > max_bytes or received > artifact["size"]:
                            raise ValueError("A public artifact exceeded the locked size or download budget.")
                        digest.update(block)
                        output.write(block)
                break
            except (URLError, TimeoutError):
                if target.exists():
                    target.unlink()  # This invocation exclusively created the wheelhouse and this partial file.
                if attempt == 2:
                    raise
                time.sleep(0.2)
        if received != artifact["size"] or "sha256:" + digest.hexdigest() != artifact["hash"]:
            raise ValueError(f"Locked hash/size mismatch for {target.name}.")
        print(f"Verified {target.name}: {received} bytes", flush=True)
        if target.suffix != ".whl":
            subprocess.run(["uv", "build", str(target), "--wheel", "--offline", "--no-build-isolation",
                            "--python", sys.executable, "--out-dir", str(wheelhouse)], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    receipt = {"extra": extra, "download_bytes": sum(item["size"] for item in plan), "transferred_bytes": transferred,
               "max_download_bytes": max_bytes, "artifacts": plan,
               "wheels": _file_receipts(wheelhouse.glob("*.whl"))}
    (wheelhouse / "closure.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def _file_receipts(paths) -> list[dict]:
    return [{"name": path.name, "bytes": path.stat().st_size,
             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in sorted(paths)]


def validate_wheelhouse(wheelhouse: Path, *, extra: str = "") -> dict:
    """Authenticate the exact local wheel closure before creating an environment."""
    from packaging.tags import sys_tags
    from packaging.utils import canonicalize_name, parse_wheel_filename
    from packaging.version import Version
    receipt_path = wheelhouse / "closure.json"
    if receipt_path.stat().st_size > 1024 * 1024:
        raise ValueError("The wheelhouse receipt exceeded its size bound.")
    receipt = json.loads(receipt_path.read_bytes())
    if receipt.get("wheels") != _file_receipts(wheelhouse.glob("*.whl")):
        raise ValueError("The wheelhouse wheels differ from their preparation receipt.")
    lock = tomllib.loads((Path(__file__).resolve().parents[1] / "uv.lock").read_text())
    budget = receipt.get("max_download_bytes")
    if type(budget) is not int or not 0 < budget <= 600 * 1024 * 1024:
        raise ValueError("The wheelhouse download budget is invalid.")
    if receipt.get("extra", "") != extra:
        raise ValueError("The wheelhouse optional extra differs from the requested closure.")
    plan = plan_artifacts(lock, _base_closure(extra=extra), [str(tag) for tag in sys_tags()], budget)
    if receipt.get("artifacts") != plan or receipt.get("download_bytes") != sum(item["size"] for item in plan):
        raise ValueError("The wheelhouse dependency closure differs from the current lock.")
    expected = {(canonicalize_name(item["name"]), Version(item["version"])) for item in plan}
    wheels = list(wheelhouse.glob("*.whl"))
    identities = []
    compatible = set(sys_tags())
    for wheel in wheels:
        name, version, _, tags = parse_wheel_filename(wheel.name)
        if wheel.is_symlink() or not tags.intersection(compatible):
            raise ValueError("The wheelhouse contains a linked or incompatible wheel.")
        identities.append((name, version))
    if len(identities) != len(expected) or set(identities) != expected:
        raise ValueError("The wheelhouse wheel names and versions differ from the exact locked closure.")
    transferred = receipt.get("transferred_bytes")
    if type(transferred) is not int or not receipt["download_bytes"] <= transferred <= budget:
        raise ValueError("The wheelhouse transferred-byte receipt is invalid.")
    for artifact in plan:
        path = wheelhouse / Path(urlsplit(artifact["url"]).path).name
        if path.is_symlink() or path.stat().st_size != artifact["size"] or \
                "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != artifact["hash"]:
            raise ValueError("The wheelhouse locked artifact hash/size differs from its receipt.")
    return _file_receipts([receipt_path])[0]


def validate_wheel_assets(wheel: Path) -> dict:
    """Check every model resource and installed license copy against its manifest."""
    prefix = "checksi/dp5q/data/"
    notices = {"LICENSE.dp5.txt", "LICENSE.cascade.txt", "LICENSE.nfp.txt", "LICENSE.nmrshiftdb2.txt", "NOTICE.txt"}
    with ZipFile(wheel) as archive:
        metadata = json.loads(archive.read(prefix + "manifest.json"))
        if set(metadata["provenance"]["notices"]) != notices or set(metadata["files"]) != {"weights.npz"}:
            raise ValueError("The wheel asset manifest does not cover all model resources.")
        inventory = metadata["files"] | metadata["provenance"]["notices"]
        actual = [name.removeprefix(prefix) for name in archive.namelist() if name.startswith(prefix)]
        if len(actual) != len(inventory) + 1 or set(actual) != set(inventory) | {"manifest.json"}:
            raise ValueError("The wheel contains unexpected or duplicate model resources.")
        for name, info in inventory.items():
            if archive.getinfo(prefix + name).file_size != info["bytes"] or \
                    hashlib.sha256(archive.read(prefix + name)).hexdigest() != info["sha256"]:
                raise ValueError(f"The wheel asset hash/size differs from the manifest: {name}.")
            if name in notices:
                copies = [entry for entry in archive.namelist() if entry.endswith(
                    ".dist-info/licenses/src/checksi/dp5q/data/" + name)]
                if len(copies) != 1 or archive.read(copies[0]) != archive.read(prefix + name):
                    raise ValueError(f"The wheel license copy differs from the model notice: {name}.")
    return inventory


def validate_installed_versions(actual: dict, expected: dict) -> None:
    """Reject missing, extra or changed distributions in the fresh installation."""
    from packaging.version import Version
    if set(actual) != set(expected) or any(Version(actual[name]) != Version(version)
                                         for name, version in expected.items()):
        raise ValueError("The installed distribution versions differ from the locked closure.")


def plant_import_probes(directory: Path) -> dict:
    """Plant controlled cwd modules so actual installed doctor/run imports prove isolation."""
    probes = {}
    for name in ("numpy", "queue", "json"):
        path = directory / (name + ".py")
        marker = directory / ("planted-" + name + "-executed")
        path.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('executed')\n"
                        "raise RuntimeError('Installed verification imported a planted cwd module')\n", encoding="utf-8")
        probes[name] = {"path": str(path), "marker": str(marker), **_file_receipts([path])[0]}
    return probes


def stage_source(source: Path, destination: Path) -> None:
    """Stage current tracked package inputs, excluding untracked files and symlinks."""
    listing = subprocess.check_output(["git", "-C", str(source), "ls-files", "-z", "--",
                                       "pyproject.toml", "README.md", "LICENSE", "src/checksi"], timeout=10)
    paths = [Path(os.fsdecode(name)) for name in listing.split(b"\0") if name]
    if not {Path(name) for name in ("pyproject.toml", "README.md", "LICENSE")}.issubset(paths):
        raise ValueError("Build staging requires tracked project metadata in a Git checkout.")
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError("Build staging requires an empty destination.")
    for relative in paths:
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Unsafe tracked build path.")
        if any((source / Path(*relative.parts[:index])).is_symlink() for index in range(1, len(relative.parts) + 1)):
            raise ValueError(f"Build staging rejects symlinked source inputs: {relative}")
        path = source / relative
        if not path.is_file():
            raise ValueError(f"Build staging requires a regular source file: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def build(source: Path, destination: Path) -> Path:
    """Build from clean, explicitly scoped source without stale build/lib or egg-info."""
    destination.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="dp5q-wheel-source-") as directory:
        stage = Path(directory)
        stage_source(source, stage)
        subprocess.run(["uv", "build", "--wheel", "--offline", "--no-build-isolation",
                        "--python", sys.executable, "--out-dir", str(destination)], cwd=stage,
                       check=True, capture_output=True, text=True, timeout=90)
    wheels = list(destination.glob("checksi-*.whl"))
    if len(wheels) != 1:
        raise ValueError("Use an output directory containing exactly one CheckSI wheel.")
    return wheels[0]


NETWORK_GUARD = '''import sys
def _no_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.bind"}:
        raise RuntimeError("Network disabled by installed-wheel verification")
sys.addaudithook(_no_network)
class _NoLegacy:
    def find_spec(self, fullname, path=None, target=None):
        blocked = ("tensorflow", "keras", "nfp", "dp5", "DP5", "dp4", "DP4", "checksi.dp5_runtime")
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise RuntimeError("Forbidden reference/legacy import: " + fullname)
sys.meta_path.insert(0, _NoLegacy())
'''


def install_verification_guard(site: Path, startup_log: Path) -> dict:
    """Bootstrap an absolute guard before sitecustomize, including in isolated children."""
    guard = site / "_checksi_dp5q_verification_guard.py"
    bootstrap = site / "000_dp5q_verification_guard.pth"
    source = NETWORK_GUARD + '''
import hashlib, json, os, socket
try:
    socket.getaddrinfo("127.0.0.1", 0, flags=socket.AI_NUMERICHOST)
except RuntimeError as exc:
    if "Network disabled" not in str(exc):
        raise
else:
    raise RuntimeError("Verification startup guard self-check failed")
with open(__file__, "rb") as source:
    digest = hashlib.sha256(source.read()).hexdigest()
arguments = sys.orig_argv
if len(arguments) > 64 or sum(len(value) for value in arguments) > 16384:
    raise RuntimeError("Verification startup argv exceeded its bound")
relevant = arguments[:1]
for index, value in enumerate(arguments[1:], 1):
    if value in ("-I", "-P", "-u", "--self-check"):
        relevant.append(value)
    elif value in ("-m", "--run-id", "--generation"):
        relevant.extend(arguments[index:index + 2])
if len(relevant) > 32 or sum(len(value) for value in relevant) > 8192:
    raise RuntimeError("Verification relevant argv exceeded its bound")
state = {"pid": os.getpid(), "ppid": os.getppid(), "orig_argv": relevant,
         "file": __file__, "sha256": digest,
         "network_self_check": True,
         "flags": {name: getattr(sys.flags, name) for name in
             ("isolated", "no_site", "no_user_site", "ignore_environment", "safe_path")}}
sys._dp5q_verification_guard = state
''' + f'''
descriptor = os.open({str(startup_log)!r}, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
try:
    os.write(descriptor, (json.dumps(state) + "\\n").encode("utf-8"))
finally:
    os.close(descriptor)
'''
    guard.write_text(source, encoding="utf-8")
    # site normally reports .pth errors and continues. A broken guard must terminate
    # before application code, rather than silently permit an unguarded probe/worker.
    loader = ("try:\n import runpy, sys\n if not hasattr(sys, '_dp5q_verification_guard'):\n  runpy.run_path(" + repr(str(guard)) + ")\n"
              "except BaseException:\n import os\n os.write(2, b'DP5q verification guard bootstrap failed\\n')\n os._exit(86)\n")
    bootstrap.write_text("import os; exec(" + repr(loader) + ")\n", encoding="utf-8")
    return {"guard": {"path": str(guard), **_file_receipts([guard])[0]},
            "bootstrap": {"path": str(bootstrap), **_file_receipts([bootstrap])[0]},
            "startup_log": str(startup_log)}


def _require_guard(manifest_path: Path | str) -> tuple[dict, dict]:
    """Refuse an absent/mismatched startup guard before importing the application."""
    custom = sys.modules.get("sitecustomize")
    provenance = {"executable": sys.executable, "prefix": sys.prefix,
                  "base_prefix": sys.base_prefix, "sitecustomize": getattr(custom, "__file__", None),
                  "flags": {name: getattr(sys.flags, name) for name in
                      ("isolated", "no_site", "no_user_site", "ignore_environment", "safe_path")}}
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        state = sys._dp5q_verification_guard
        assert sys.flags.isolated and sys.flags.safe_path and state["network_self_check"]
        for name in ("guard", "bootstrap"):
            expected = manifest[name]
            path = Path(expected["path"])
            assert path.stat().st_size == expected["bytes"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == expected["sha256"]
        assert state["file"] == manifest["guard"]["path"]
        assert state["sha256"] == manifest["guard"]["sha256"]
    except (OSError, ValueError, TypeError, KeyError, AttributeError, AssertionError) as exc:
        raise RuntimeError("Verification startup guard absent or mismatched; " + json.dumps(provenance)) from exc
    return manifest, provenance


def read_guard_startups(manifest: dict) -> list[dict]:
    """Validate bounded activation receipts from the actual parent and model children."""
    path = Path(manifest["startup_log"])
    if path.stat().st_size > 64 * 1024:
        raise ValueError("Verification startup receipts exceeded their bound")
    starts = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if not starts or len(starts) > 64:
        raise ValueError("Verification startup receipt count is invalid")
    for state in starts:
        if (state["file"] != manifest["guard"]["path"] or
                state["sha256"] != manifest["guard"]["sha256"] or not state["network_self_check"]):
            raise ValueError("Verification child startup guard does not match")
        if state.get("flags", {}).get("safe_path") is not True:
            raise ValueError("Verification doctor/run startup must enable safe_path")
    return starts


def match_worker_startup(starts: list[dict], launcher_pid: int, run_id: str, generation: int) -> dict:
    """Bind one guarded interpreter to its exact request and direct/redirector PID."""
    matches = []
    for state in starts:
        argv = state.get("orig_argv", [])
        if (not isinstance(argv, list) or len(argv) > 32 or
                any(not isinstance(value, str) or len(value) > 4096 for value in argv) or
                sum(len(value) for value in argv) > 8192 or
                type(state.get("pid")) is not int or type(state.get("ppid")) is not int):
            continue
        def argument(name):
            if argv.count(name) != 1:
                return None
            index = argv.index(name)
            return argv[index + 1] if index + 1 < len(argv) else None
        if (argument("-m") == "checksi.dp5q.worker" and
                argument("--run-id") == run_id and argument("--generation") == str(generation) and
                (state.get("pid") == launcher_pid or state.get("ppid") == launcher_pid)):
            matches.append(state)
    if len(matches) != 1:
        raise RuntimeError("Worker startup identity mismatch: " + json.dumps({
            "expected": {"launcher_pid": launcher_pid, "module": "checksi.dp5q.worker",
                         "run_id": run_id, "generation": generation}, "guard_startups": starts}))
    return matches[0]


class NativeProcessObservation:
    """Observe one validated live interpreter; never signal it or request terminate access."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.handle = None
        self.kernel = None
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            self.kernel.OpenProcess.restype = wintypes.HANDLE
            self.kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            self.kernel.WaitForSingleObject.restype = wintypes.DWORD
            self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            self.kernel.CloseHandle.restype = wintypes.BOOL
            self.handle = self.kernel.OpenProcess(0x00100000 | 0x1000, False, pid)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
        try:
            live = self.is_running()
        except BaseException:
            self.close()
            raise
        if not live:
            self.close()
            raise RuntimeError(f"Actual interpreter {pid} was not live when observed")

    def __enter__(self) -> NativeProcessObservation:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def is_running(self) -> bool:
        if self.kernel is not None:
            import ctypes
            result = self.kernel.WaitForSingleObject(self.handle, 0)
            if result == 0x102:
                return True
            if result == 0:
                return False
            if result == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            raise RuntimeError(f"Unexpected process wait result {result}")
        result = subprocess.run(["ps", "-o", "pid=", "-p", str(self.pid)],
                                capture_output=True, text=True, timeout=5, check=False)
        if result.returncode not in (0, 1):
            raise RuntimeError("Actual interpreter query failed: " + result.stderr)
        return bool(result.stdout.strip())

    def wait_gone(self, timeout: float = 5) -> dict:
        started = time.monotonic()
        while self.is_running():
            if time.monotonic() - started >= timeout:
                raise RuntimeError(f"Actual interpreter {self.pid} remains alive after cleanup")
            time.sleep(0.02)
        return {"pid": self.pid, "gone": True, "seconds": time.monotonic() - started,
                "query": "retained SYNCHRONIZE/QUERY_LIMITED_INFORMATION handle" if self.kernel else "ps -o pid= -p"}

    def close(self) -> None:
        if self.handle:
            import ctypes
            if not self.kernel.CloseHandle(self.handle):
                raise ctypes.WinError(ctypes.get_last_error())
            self.handle = None


def _native_metrics(pid: int) -> dict:
    """Inspect only the owned live worker; report unavailable measurements explicitly."""
    try:
        if sys.platform == "darwin":
            threads = subprocess.check_output(["ps", "-M", "-p", str(pid)], text=True, timeout=5)
            rss = subprocess.check_output(["ps", "-o", "rss=", "-p", str(pid)], text=True, timeout=5)
            return {"threads": len(threads.strip().splitlines()) - 1, "rss_bytes": int(rss) * 1024,
                    "source": "ps -M and ps -o rss"}
        if sys.platform.startswith("linux"):
            fields = dict(line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines() if ":" in line)
            return {"threads": int(fields["Threads"]), "rss_bytes": int(fields["VmRSS"].split()[0]) * 1024,
                    "peak_rss_bytes": int(fields["VmHWM"].split()[0]) * 1024, "source": "/proc/pid/status"}
        if sys.platform == "win32":
            raw = subprocess.check_output(["powershell", "-NoProfile", "-Command",
                f"$p = Get-Process -Id {pid}; @{{threads=$p.Threads.Count; rss_bytes=$p.WorkingSet64; peak_rss_bytes=$p.PeakWorkingSet64}} | ConvertTo-Json"],
                text=True, timeout=10)
            return {**json.loads(raw), "source": "Get-Process"}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"measurement_error": str(exc)}
    return {"measurement_error": "No native measurement implementation for this platform."}


def _native_cancel_request():
    """Use an eligible asymmetric engineering load, without a score expectation."""
    from checksi.dp5q.contracts import CheckRequest
    return CheckRequest(compound_id="cancel-native", smiles="C" * 30 + "O", carbon_shifts=list(range(30)))


def _cancel_on_geometry(session, token, observation, compound_id: str):
    """Bind cancellation to the next real request, after discarding warmup progress."""
    from checksi.events import ProgressEvent
    generation = session._generation
    metadata = session.metadata
    assert generation is not None and generation.ready_seen
    with generation.lock:
        assert generation.sequence >= 1 and generation.terminal_sequence == generation.sequence
        generation.progress = None
        sequence = generation.sequence + 1
    state = {"compound_id": compound_id, "run_id": metadata["run_id"],
             "generation": metadata["generation"], "request_sequence": sequence,
             "previous_terminal_sequence": sequence - 1}

    def cancel_at_geometry(event):
        if not isinstance(event, ProgressEvent) or event.message != "DP5q: geometry":
            return
        assert session._generation is generation, "Cancellation worker generation was replaced"
        if generation.sequence != sequence:
            return  # A warmup/other sequence is never current-request evidence.
        assert generation.process.poll() is None and observation.is_running(), "Cancellation worker is not live"
        with generation.lock:
            assert session._sequence == generation.sequence == sequence
            assert generation.run_id == state["run_id"] and generation.number == state["generation"]
            assert not generation.failed.is_set() and generation.terminal_sequence < sequence, "Cancellation request already terminated"
            state["geometry_progress"] = {"message": event.message, "run_id": generation.run_id,
                "generation": generation.number, "request_sequence": sequence,
                "terminal_sequence": generation.terminal_sequence, "interpreter_pid": observation.pid,
                "launcher_pid": generation.process.pid, "worker_alive": True, "request_inflight": True}
            state["cancel_requested_at"] = time.perf_counter()
            token.set()

    return cancel_at_geometry, state


def probe(guard_manifest: Path | str) -> dict:
    """Run real installed models and runner paths with network/legacy imports blocked."""
    manifest, startup_provenance = _require_guard(guard_manifest)
    import socket
    from unittest.mock import patch
    import checksi
    from checksi.config_schema import AppConfig, DP5Settings
    from checksi.dp5q.client import DP5qCancelled, DP5qSession, check_availability, doctor_runtime
    from checksi.dp5q.contracts import CheckRequest

    installed = Path(checksi.__file__).resolve()
    assert Path(sys.prefix).resolve() in installed.parents, installed
    assert not (installed.parent / "dp5_runtime.py").exists(), "Legacy runtime remains in the wheel"
    versions = {re.sub(r"[-_.]+", "-", d.metadata["Name"].lower()): d.version
                for d in importlib.metadata.distributions()}
    names = set(versions)
    assert not names.intersection({"tensorflow", "tensorflow-macos", "keras", "nfp", "pyside6", "qt-material", "dp5", "dp4"})
    try:
        socket.getaddrinfo("127.0.0.1", 0, flags=socket.AI_NUMERICHOST)
    except RuntimeError as exc:
        assert "Network disabled" in str(exc)
    else:
        raise AssertionError("Network guard is not active")
    metadata = check_availability()
    started = time.perf_counter()
    doctor_ok, doctor_messages = doctor_runtime()
    doctor_seconds = time.perf_counter() - started
    assert doctor_ok, doctor_messages
    doctor_rows = [state for state in read_guard_startups(manifest)
                   if state["orig_argv"].count("--self-check") == 1 and
                   "checksi.dp5q.worker" in state["orig_argv"] and
                   "--run-id" not in state["orig_argv"] and "--generation" not in state["orig_argv"]]
    assert len(doctor_rows) == 1, doctor_rows
    requests = [CheckRequest(compound_id="ethanol", smiles="CCO", carbon_shifts=[60.0, 20.0]),
                CheckRequest(compound_id="propanol", smiles="CCCO", carbon_shifts=[10.0, 25.0, 60.0])]
    started = time.perf_counter()
    with DP5qSession() as session:
        first = session.check(requests[0])
        first_seconds = time.perf_counter() - started
        launcher_pid = session._processes[0].pid
        started = time.perf_counter()
        second = session.check(requests[1])
        second_seconds = time.perf_counter() - started
        assert first.status == second.status == "assessed", (first, second)
        assert len(session._processes) == 1 and session._processes[0].pid == launcher_pid
        normal_identity = match_worker_startup(read_guard_startups(manifest), launcher_pid,
                                               session.metadata["run_id"], session.metadata["generation"])
        with NativeProcessObservation(normal_identity["pid"]) as observation:
            metrics = {"pid": normal_identity["pid"], **_native_metrics(normal_identity["pid"])}
            assert "measurement_error" not in metrics, metrics
            close_started = time.perf_counter()
            session.close()
            normal_exit = observation.wait_gone(timeout=max(.05, 5 - (time.perf_counter() - close_started)))
            normal_exit["cleanup_seconds"] = time.perf_counter() - close_started
            assert normal_exit["cleanup_seconds"] < 5, normal_exit
    assert all(child.poll() is not None for child in session._processes)

    token = threading.Event()
    with DP5qSession(cancel_token=token) as cancelled:
        warmup = cancelled.check(requests[0])  # Cancellation below targets an already READY real worker.
        assert warmup.status == "assessed", (warmup.status, warmup.reason)
        cancel_launcher_pid = cancelled._processes[0].pid
        cancel_identity = match_worker_startup(read_guard_startups(manifest), cancel_launcher_pid,
                                               cancelled.metadata["run_id"], cancelled.metadata["generation"])
        with NativeProcessObservation(cancel_identity["pid"]) as observation:
            cancel_request = _native_cancel_request()
            callback, cancellation_witness = _cancel_on_geometry(cancelled, token, observation, cancel_request.compound_id)
            cancelled.event_dispatcher = callback
            try:
                terminal = cancelled.check(cancel_request)
            except DP5qCancelled:
                assert cancellation_witness.get("geometry_progress"), "No current geometry progress preceded cancellation"
                cancellation_seconds = time.perf_counter() - cancellation_witness["cancel_requested_at"]
                cancellation_witness.update(outcome="DP5qCancelled", warmup_status=warmup.status,
                                            warmup_compound_id=requests[0].compound_id)
            else:
                raise AssertionError(f"Cancellation fixture returned terminal status={terminal.status!r}, "
                                     f"reason={terminal.reason!r}; witness={cancellation_witness!r}")
            finally:
                cancelled.event_dispatcher = None
            assert cancellation_seconds < 5, cancellation_seconds
            assert len(cancelled._processes) == 1 and cancelled._processes[0].pid == cancel_launcher_pid
            cancelled.close()
            cancel_exit = observation.wait_gone(timeout=max(.05, 5 - cancellation_seconds))
            assert time.perf_counter() - cancellation_witness["cancel_requested_at"] < 5
    assert all(child.poll() is not None for child in cancelled._processes)

    import fitz
    from checksi.application_runner import ApplicationRunner
    with tempfile.TemporaryDirectory(prefix="dp5q-runner-proof-") as directory:
        root = Path(directory)
        source = root / "input.pdf"
        with fitz.open() as document:
            document.new_page().insert_text((72, 72), "General methods.")
            document.save(source)
        config = AppConfig.model_validate({
            "paths": {"input_path": str(source), "output_dir": str(root / "reports")},
            "modes": {"deep_name_analysis": "off", "output_mode": "essential"},
            "solvents": {"entries": []}, "errors": {"default_severity": "warning"},
        })
        runner_receipts = []
        for enabled in (False, True):
            config.dp5 = DP5Settings(enabled=enabled)
            with patch("checksi.dp5q.client.subprocess.Popen", wraps=subprocess.Popen) as spawn:
                result = ApplicationRunner(event_dispatcher=lambda event: None).execute(config, str(source))
            assert spawn.call_count == 0, "Disabled/no-eligible run started a subprocess"
            assert result.analysis_data["total_found"] == 0
            keys = ("html_path", "analysis_path", "pdf_path", "error_stats_path")
            assert not result.report_data["partial"], result.report_data["artifact_warnings"]
            assert all(Path(result.report_data[key]).is_file() for key in keys)
            runner_receipts.append({"enabled": enabled, "worker_starts": spawn.call_count,
                "outputs": _file_receipts(Path(result.report_data[key]) for key in keys)})
    assert not any(name.startswith("PySide6") for name in sys.modules)
    assert "checksi.dp5q.network" not in sys.modules, "Parent loaded the numerical model"
    cwd_probes = {}
    for name in ("numpy", "queue", "json"):
        path = Path.cwd() / (name + ".py")
        marker = Path.cwd() / ("planted-" + name + "-executed")
        assert path.is_file() and not marker.exists(), "Planted cwd module was missing or executed: " + name
        cwd_probes[name] = _file_receipts([path])[0]
    starts = read_guard_startups(manifest)
    expected_pids = {os.getpid(), doctor_rows[0]["pid"], normal_identity["pid"], cancel_identity["pid"]}
    assert len(starts) == len(expected_pids) == 4 and expected_pids == {state["pid"] for state in starts}, starts
    return {"python": sys.version, "platform": platform.platform(), "machine": platform.machine(),
            "installed_package": str(installed), "metadata": metadata,
            "doctor": doctor_messages, "doctor_seconds_including_cold_imports": doctor_seconds,
            "first_seconds_including_startup": first_seconds,
            "second_seconds_reusing_worker": second_seconds, "worker_process_count": 1,
            "native_worker": metrics, "results": [{"status": result.status, "score_raw": result.score_raw,
                "timings": result.timings} for result in (first, second)],
            "cancellation_seconds": cancellation_seconds, "all_workers_reaped": True,
            "cancellation_request": cancel_request.model_dump(mode="json"),
            "cancellation_witness": cancellation_witness,
            "runner_paths": runner_receipts, "network": "Python socket audit guard active in parent and children",
            "base_distributions": sorted(names), "base_distribution_versions": versions,
            "startup_provenance": startup_provenance,
            "guard_manifest": manifest, "guard_startups": starts,
            "cwd_import_probes": {"modules": cwd_probes, "executed": []},
            "process_identities": {
                "normal": {"launcher_pid": launcher_pid, "interpreter": normal_identity, "exit": normal_exit},
                "cancel": {"launcher_pid": cancel_launcher_pid, "interpreter": cancel_identity, "exit": cancel_exit},
                "doctor": {"interpreter": doctor_rows[0],
                           "scope": "Unique guarded --self-check activation in this fresh environment; doctor API does not expose its launcher PID."}}}


def verify(wheel: Path, wheelhouse: Path) -> dict:
    """Install solely from local wheels into a fresh environment outside the checkout."""
    closure_receipt = validate_wheelhouse(wheelhouse)
    model_receipts = validate_wheel_assets(wheel)
    with ZipFile(wheel) as archive:
        assert "checksi/dp5_runtime.py" not in archive.namelist()
        assert "checksi/dp5q/data/weights.npz" in archive.namelist()
    with tempfile.TemporaryDirectory(prefix="dp5q-installed-") as directory:
        root = Path(directory)
        environment = root / "environment"
        env = {key: value for key, value in os.environ.items()
               if key not in {"PYTHONPATH", "PYTHONHOME", "PYTHONSAFEPATH", "VIRTUAL_ENV"}}
        env.update(UV_OFFLINE="1", UV_NO_CACHE="1", PYTHONNOUSERSITE="1")
        subprocess.run(["uv", "venv", "--python", sys.executable, str(environment)], cwd=root, env=env,
                       check=True, capture_output=True, text=True, timeout=30)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        install = subprocess.run(["uv", "pip", "install", "--python", str(python), "--offline", "--no-index",
                                  "--no-cache", "--find-links", str(wheelhouse), str(wheel)],
                                 cwd=root, env=env, check=True, capture_output=True, text=True, timeout=90)
        site = subprocess.check_output([str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                                       cwd=root, env=env, text=True, timeout=10).strip()
        manifest = install_verification_guard(Path(site), root / "guard-startups.jsonl")
        manifest_path = root / "guard-manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        plant_import_probes(root)
        completed = subprocess.run([str(python), "-I", str(Path(__file__).resolve()), "probe",
                                    "--guard-manifest", str(manifest_path)],
                                   cwd=root, env=env, capture_output=True, text=True, timeout=300)
        if completed.returncode:
            raise RuntimeError(completed.stdout + completed.stderr)
        receipt = json.loads(completed.stdout)
        from packaging.utils import parse_wheel_filename
        expected_versions = {**_base_closure(), "checksi": str(parse_wheel_filename(wheel.name)[1])}
        validate_installed_versions(receipt["base_distribution_versions"], expected_versions)
        receipt.update(wheel=_file_receipts([wheel])[0], dependency_wheels=_file_receipts(wheelhouse.glob("*.whl")),
                       closure_receipt=closure_receipt, model_resources=model_receipts,
                       verification_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                       environment_file_bytes=sum(p.stat().st_size for p in environment.rglob("*") if p.is_file() and not p.is_symlink()),
                       installation_log=install.stderr,
                       interpreter_note="Environment size excludes the externally managed CPython installation.")
        return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="Download only the locked native base closure from public PyPI.")
    prep.add_argument("--wheelhouse", type=Path, required=True)
    prep.add_argument("--max-mib", type=int, default=600)
    wheel = commands.add_parser("build", help="Build one wheel from clean staged source, offline.")
    wheel.add_argument("--out-dir", type=Path, required=True)
    check = commands.add_parser("verify", help="Install local wheels offline and run isolated real model checks.")
    check.add_argument("--wheel", type=Path, required=True)
    check.add_argument("--wheelhouse", type=Path, required=True)
    check.add_argument("--receipt", type=Path, required=True)
    probe_command = commands.add_parser("probe", help=argparse.SUPPRESS)
    probe_command.add_argument("--guard-manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        if not 1 <= args.max_mib <= 600:
            parser.error("--max-mib must be between 1 and 600")
        receipt = prepare(args.wheelhouse.resolve(), args.max_mib * 1024 * 1024)
        print(f"Verified public download closure: {receipt['download_bytes']} bytes")
    elif args.command == "build":
        print(build(Path(__file__).resolve().parents[1], args.out_dir.resolve()))
    elif args.command == "verify":
        if args.receipt.exists():
            parser.error("The receipt already exists; choose a new immutable output path")
        receipt = verify(args.wheel.resolve(), args.wheelhouse.resolve())
        with args.receipt.open("x", encoding="utf-8") as output:
            json.dump(receipt, output, indent=2, allow_nan=False)
            output.write("\n")
        print(args.receipt)
    else:
        print(json.dumps(probe(args.guard_manifest), allow_nan=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(exc.stdout or "", file=sys.stderr)
        print(exc.stderr or "", file=sys.stderr)
        raise SystemExit(exc.returncode)
