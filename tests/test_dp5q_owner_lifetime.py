"""The real owner, rather than a live interpreter launcher, bounds cold startup."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from checksi.dp5q.client import worker_environment
from checksi.dp5q import client
from checksi.dp5q.contracts import CheckRequest
from checksi.dp5q.worker import _OwnerProcess


class OwnerLifetimeTests(unittest.TestCase):
    def test_explicit_owner_environment_is_preserved_by_child_thread_limits(self):
        with patch.dict(os.environ, {"DP5Q_OWNER_PID": "41"}):
            self.assertEqual(worker_environment()["DP5Q_OWNER_PID"], "41")
            self.assertEqual(worker_environment(owner_pid=42)["DP5Q_OWNER_PID"], "42")

    def test_worker_and_doctor_launches_pin_the_actual_owner_pid(self):
        original = subprocess.Popen
        owners = []
        def start(*args, **kwargs):
            owners.append(kwargs["env"].get("DP5Q_OWNER_PID"))
            return original(*args, **kwargs)
        with patch.dict(os.environ, {"DP5Q_OWNER_PID": "1"}), patch.object(client.subprocess, "Popen", side_effect=start):
            with client.DP5qSession() as session:
                self.assertEqual(session.check(CheckRequest(compound_id="owner", smiles="CCO", carbon_shifts=(18.5, 58.))).status, "assessed")
            ready, messages = client.doctor_runtime()
            self.assertTrue(ready, messages)
        self.assertEqual(owners, [str(os.getpid()), str(os.getpid())])

    def test_windows_owner_handle_uses_only_explicit_pid_and_synchronization_rights(self):
        kernel = SimpleNamespace(OpenProcess=Mock(return_value=2 ** 40),
            WaitForSingleObject=Mock(side_effect=(0x102, 0)), CloseHandle=Mock(return_value=1))
        with patch("checksi.dp5q.worker.os.getppid", return_value=999):
            owner = _OwnerProcess(42, _kernel=kernel)
        try:
            kernel.OpenProcess.assert_called_once_with(0x00100000, False, 42)
            self.assertTrue(owner.is_alive())
            self.assertFalse(owner.is_alive())
            self.assertEqual(kernel.WaitForSingleObject.call_args.args, (2 ** 40, 0))
        finally:
            owner.close()
            owner.close()
        kernel.CloseHandle.assert_called_once_with(2 ** 40)

    def test_windows_failed_owner_wait_is_not_treated_as_alive(self):
        kernel = SimpleNamespace(OpenProcess=Mock(return_value=10),
            WaitForSingleObject=Mock(return_value=0xFFFFFFFF), CloseHandle=Mock(return_value=1))
        owner = _OwnerProcess(42, _kernel=kernel)
        try:
            with self.assertRaises(OSError):
                owner.is_alive()
        finally:
            owner.close()
        kernel.OpenProcess.return_value = None
        owner = _OwnerProcess(42, _kernel=kernel)
        self.assertFalse(owner.is_alive())
        owner.close()

    @unittest.skipUnless(os.name == "posix", "POSIX direct parent replacement")
    def test_direct_parent_replacement_detects_an_unreaped_owner(self):
        with patch("checksi.dp5q.worker.os.getppid", side_effect=(42, 1)), patch(
            "checksi.dp5q.worker.os.kill", side_effect=AssertionError("must not query zombie PID"),
        ):
            self.assertFalse(_OwnerProcess(42).is_alive())

    def test_owner_exit_during_import_stops_worker_while_redirector_and_stdin_stay_live(self):
        fixture = Path(__file__).parent / "fixtures/dp5q/failing_engine_worker.py"
        intermediate = textwrap.dedent("""\
            import json, os, subprocess, sys, time
            from pathlib import Path
            root = Path(sys.argv[1])
            def publish(name, value):
                temporary = root / (name + '.tmp')
                temporary.write_text(json.dumps(value))
                temporary.replace(root / name)
            env = dict(os.environ, DP5Q_OWNER_PID=sys.argv[2])
            child = subprocess.Popen([sys.executable, '-u', sys.argv[3], 'blocked_import',
                '--run-id', 'cold-owner', '--generation', '1'], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
            try:
                assert child.stderr.readline().strip() == b'BLOCKED-IMPORT'
                publish('ready.json', {'worker': child.pid, 'redirector': os.getpid()})
                try:
                    child.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=2)
                publish('worker-exit.json', {'exit': child.returncode})
                deadline = time.monotonic() + 10
                count = 0
                while not (root / 'stop').exists() and time.monotonic() < deadline:
                    (root / ('heartbeat-' + str(count))).touch()
                    count += 1
                    time.sleep(.05)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=2)
                child.stdin.close()
                child.stderr.close()
                publish('redirector-exit', {'done': True})
            """)
        owner = textwrap.dedent("""\
            import os, subprocess, sys, time
            from pathlib import Path
            root = Path(sys.argv[1])
            subprocess.Popen([sys.executable, '-c', sys.argv[2], str(root), str(os.getpid()), sys.argv[3]],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 15
            while not (root / 'ready.json').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            assert (root / 'ready.json').exists(), 'worker never reached blocked import'
            print((root / 'ready.json').read_text(), flush=True)
            os._exit(0)
            """)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            try:
                completed = subprocess.run([sys.executable, "-c", owner, directory, intermediate, str(fixture)],
                    capture_output=True, text=True, timeout=20, check=False)
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                identities = json.loads(completed.stdout)
                self.assertNotEqual(identities["worker"], identities["redirector"])
                deadline = time.monotonic() + 2.5
                while not (root / "worker-exit.json").exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue((root / "worker-exit.json").exists(), "Cold worker outlived its actual owner.")
                self.assertEqual(json.loads((root / "worker-exit.json").read_text())["exit"], 0)
                deadline = time.monotonic() + 1
                while not list(root.glob("heartbeat-*")) and time.monotonic() < deadline:
                    time.sleep(.02)
                first = len(list(root.glob("heartbeat-*")))
                deadline = time.monotonic() + 1
                while len(list(root.glob("heartbeat-*"))) <= first and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertGreater(len(list(root.glob("heartbeat-*"))), first, "Intermediate parent must remain live.")
            finally:
                (root / "stop").write_text("stop")
                deadline = time.monotonic() + 9
                while not (root / "redirector-exit").exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue((root / "redirector-exit").exists(), "Owned redirector did not finish cleanup.")


if __name__ == "__main__":
    unittest.main()
