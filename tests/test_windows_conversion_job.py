"""Native Windows process/Job Object regressions; no office suite is needed."""

from __future__ import annotations

import ctypes as ct
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from docx import Document

from checksi.utils import conversion_supervisor
from checksi.utils.windows_job import WindowsJobProcess


def wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


@unittest.skipUnless(os.name == "nt", "Native Windows Job Object process ownership")
class WindowsConversionJobTests(unittest.TestCase):
    """Exercise the actual kernel job, Unicode CreateProcessW and owner EOF."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="conversion job café Ω "))).resolve()
        self.kernel = ct.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = (ct.c_uint32, ct.c_int, ct.c_uint32)
        self.kernel.OpenProcess.restype = ct.c_void_p
        self.kernel.WaitForSingleObject.argtypes = (ct.c_void_p, ct.c_uint32)
        self.kernel.WaitForSingleObject.restype = ct.c_uint32
        self.kernel.TerminateProcess.argtypes = (ct.c_void_p, ct.c_uint32)
        self.kernel.TerminateProcess.restype = ct.c_int
        self.kernel.CloseHandle.argtypes = (ct.c_void_p,)
        self.kernel.CloseHandle.restype = ct.c_int
        self.handles = []

    def tearDown(self):
        for handle in self.handles:
            if self.kernel.WaitForSingleObject(handle, 0) == 0x102:
                self.kernel.TerminateProcess(handle, 1)
                self.kernel.WaitForSingleObject(handle, 2000)
            self.kernel.CloseHandle(handle)

    def open_process(self, pid):
        handle = self.kernel.OpenProcess(0x00100001, False, pid)  # synchronize + terminate own fixture only
        self.assertTrue(handle, f"cannot observe owned fixture PID {pid}")
        self.handles.append(handle)
        return handle

    def fixture_code(self, *, exit_launcher=False):
        return (
            "import json,os,pathlib,subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
            "target=pathlib.Path(sys.argv[1]); temporary=target.with_suffix('.tmp'); "
            "temporary.write_text(json.dumps({'launcher':os.getpid(),'child':child.pid})); "
            "temporary.replace(target); " + ("sys.exit(0)" if exit_launcher else "time.sleep(60)")
        )

    def test_job_stops_descendant_after_successful_launcher_exit(self):
        marker = self.root / "pids.json"
        process = WindowsJobProcess([sys.executable, "-c", self.fixture_code(exit_launcher=True), str(marker)], cwd=self.root)
        try:
            self.assertTrue(wait_until(marker.is_file))
            self.assertTrue(wait_until(lambda: process.poll() == 0))
            child = self.open_process(json.loads(marker.read_text(encoding="utf-8"))["child"])
            self.assertEqual(self.kernel.WaitForSingleObject(child, 0), 0x102)
            process.stop_tree()
            self.assertEqual(self.kernel.WaitForSingleObject(child, 3000), 0)
            self.assertEqual(process.wait(timeout=2), 0)
        finally:
            process.close()

    def test_closing_last_job_handle_kills_a_running_descendant(self):
        marker = self.root / "pids.json"
        process = WindowsJobProcess([sys.executable, "-c", self.fixture_code(), str(marker)], cwd=self.root)
        try:
            self.assertTrue(wait_until(marker.is_file))
            pids = json.loads(marker.read_text(encoding="utf-8"))
            handles = [self.open_process(pids[key]) for key in ("launcher", "child")]
            process.close()
            for handle in handles:
                self.assertEqual(self.kernel.WaitForSingleObject(handle, 3000), 0)
        finally:
            process.close()

    def prepare_request(self, seconds):
        source = self.root / "source Ω.DOCX"
        Document().save(source)
        attempt = Path(tempfile.mkdtemp(prefix="docx-pdf-", dir=self.root))
        marker = attempt / "pids.json"
        request = {"source": str(source), "attempt": str(attempt),
                   "command": [sys.executable, "-c", self.fixture_code(), str(marker)],
                   "deadline": time.monotonic() + seconds}
        request_path = attempt / "request.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        return attempt, marker, request_path

    def test_actual_owner_termination_closes_lease_and_cleans_job_and_private_copy(self):
        attempt, marker, request = self.prepare_request(30)
        owner_marker = self.root / "owner.pid"
        owner_code = (
            "import os,pathlib,subprocess,sys,time; "
            "pathlib.Path(sys.argv[3]).write_text(str(os.getpid())); "
            "child=subprocess.Popen([sys.executable,'-I','-u',sys.argv[1],sys.argv[2]],"
            "stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=0x08000200); "
            "time.sleep(60)"
        )
        owner = subprocess.Popen([sys.executable, "-c", owner_code, conversion_supervisor.__file__,
                                  str(request), str(owner_marker)], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.assertTrue(wait_until(marker.is_file), "native supervised fixture never started")
            actual_owner = self.open_process(int(owner_marker.read_text(encoding="utf-8")))
            pids = json.loads(marker.read_text(encoding="utf-8"))
            children = [self.open_process(pids[key]) for key in ("launcher", "child")]
            self.assertTrue((attempt / "input.docx").is_file())
            self.assertTrue(self.kernel.TerminateProcess(actual_owner, 1))
            for handle in children:
                self.assertEqual(self.kernel.WaitForSingleObject(handle, 5000), 0)
            self.assertTrue(wait_until(lambda: not attempt.exists()), "orphaned private copy was retained")
        finally:
            if owner.poll() is None:
                owner.kill()
            owner.wait(timeout=3)

    def test_supervisor_deadline_stops_job_while_owner_pipe_remains_open(self):
        attempt, marker, request = self.prepare_request(2)
        process = subprocess.Popen([sys.executable, "-I", "-u", conversion_supervisor.__file__, str(request)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=0x08000200)
        try:
            self.assertTrue(wait_until(marker.is_file), "native deadline fixture never started")
            children = [self.open_process(pid) for pid in json.loads(marker.read_text(encoding="utf-8")).values()]
            process.wait(timeout=8)
            result = json.loads(process.stdout.read(16385))
            self.assertEqual(result["status"], "failed")
            self.assertIn("timed out", result["message"])
            self.assertFalse(process.stdin.closed)
            self.assertFalse(attempt.exists())
            for handle in children:
                self.assertEqual(self.kernel.WaitForSingleObject(handle, 0), 0)
        finally:
            process.stdin.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            process.stdout.close()
            process.stderr.close()


if __name__ == "__main__":
    unittest.main()
