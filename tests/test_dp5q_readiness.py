"""Lightweight readiness does not start or load the numerical models."""

import json
import io
from pathlib import Path
import re
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from checksi.dp5q import client
from checksi.dp5q.client import (DP5qAvailabilityError, DP5qWorkerUnavailable, _run_diagnostics,
                                    check_availability, doctor_runtime, worker_environment)


class DP5qReadinessTests(unittest.TestCase):
    def test_worker_missing_or_unsupported_observation_capability_is_rejected(self):
        from pathlib import Path
        from checksi.dp5q.contracts import CheckRequest
        fixture = Path(__file__).parent / 'fixtures/dp5q/controlled_worker.py'
        ready = {'model_id': 'fixture', 'score_version': 'score', 'preprocessing_id': 'graph',
                 'geometry_policy': 'single', 'weights_sha256': 'a' * 64}
        for mode in ('missing_capability', 'omission_capability'):
            with self.subTest(mode=mode), patch.object(client, 'check_availability', return_value=ready):
                with client.DP5qSession(_command=[sys.executable, '-u', str(fixture), mode]) as session:
                    with self.assertRaises(DP5qWorkerUnavailable) as raised:
                        session.check(CheckRequest(compound_id='capability', smiles='CCO', carbon_shifts=[10., 20.]))
                    self.assertEqual(raised.exception.cause, 'protocol_error')
                self.assertTrue(all(process.poll() is not None for process in session._processes))

    def metadata(self):
        return {"model_id": "fixture", "score_id": "score", "preprocessing_id": "graph",
                "geometry_policy": "single", "files": {"weights.npz": {"sha256": "a" * 64}}}

    def test_availability_only_reads_metadata(self):
        with patch("checksi.dp5q.client.validate_asset_metadata", return_value=self.metadata()), \
             patch("checksi.dp5q.client.importlib.metadata.version", return_value="1"), \
             patch("checksi.dp5q.client.subprocess.Popen") as start:
            info = check_availability()
        self.assertTrue(info["available"])
        self.assertEqual(info["score_version"], "score")
        start.assert_not_called()

    def test_asset_failure_links_to_existing_recovery_documentation(self):
        from scripts.check_documentation import parse_markdown

        with patch.object(client, "validate_asset_metadata", side_effect=client.AssetError("Controlled asset failure")):
            with self.assertRaises(DP5qAvailabilityError) as raised:
                check_availability()
        urls = re.findall(r"https://[^\s]+", str(raised.exception))
        self.assertEqual(len(urls), 1, str(raised.exception))
        url = urlsplit(urls[0])
        self.assertEqual(url.netloc, "github.com")
        self.assertTrue(url.path.startswith("/aelashkin/CheckSI/blob/main/"))
        guide = url.path.split("/blob/main/", 1)[1]
        self.assertEqual(guide, "docs/installation.md")
        path = Path(__file__).resolve().parents[1] / guide
        self.assertIn(url.fragment, parse_markdown(path.read_text(encoding="utf-8")).anchors)

    def test_manual_doctor_checks_owned_worker(self):
        with patch("checksi.dp5q.client.check_availability", return_value={"model_id": "fixture"}), \
             patch("checksi.dp5q.client._run_diagnostics", return_value=(
                 0, json.dumps({"ok": True, "messages": ["finite predictions"]}), "")) as run:
            ok, messages = doctor_runtime()
        self.assertTrue(ok)
        self.assertIn("finite predictions", messages)
        self.assertIn("checksi.dp5q.worker", run.call_args.args[0])

    def test_missing_ready_identifiers_are_an_availability_error(self):
        metadata = self.metadata()
        del metadata["score_id"]
        with patch("checksi.dp5q.client.validate_asset_metadata", return_value=metadata), \
             patch("checksi.dp5q.client.importlib.metadata.version", return_value="1"):
            with self.assertRaises(DP5qAvailabilityError):
                check_availability()

    def test_doctor_rejects_nonfinite_and_nontext_diagnostics(self):
        for raw in ('{"ok":true,"messages":[NaN]}', '{"ok":true,"messages":"bad"}',
                    '{"ok":true,"messages":[1]}', '{"ok":true,"messages":[1e999]}'):
            with self.subTest(raw=raw):
                with patch("checksi.dp5q.client.check_availability", return_value={"model_id": "fixture"}), \
                     patch("checksi.dp5q.client._run_diagnostics", return_value=(0, raw, "")):
                    self.assertFalse(doctor_runtime()[0])

    def test_doctor_failure_uses_structured_message_ahead_of_unrelated_stderr(self):
        with patch("checksi.dp5q.client.check_availability", return_value={"model_id": "fixture"}), patch(
            "checksi.dp5q.client._run_diagnostics",
            return_value=(2, json.dumps({"ok": False, "messages": ["Archive hash is invalid."]}), "unrelated warning"),
        ):
            ok, messages = doctor_runtime()
        self.assertFalse(ok)
        self.assertEqual(messages, ["DP5q worker self-check failed: Archive hash is invalid."])

    def test_real_doctor_refuses_broken_engine_dependency(self):
        from pathlib import Path
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            scipy = Path(directory) / "scipy"
            scipy.mkdir()
            (scipy / "__init__.py").write_text("raise ImportError('controlled broken scipy extension')\n")
            search_path = os.pathsep.join(filter(None, (directory, os.environ.get("PYTHONPATH", ""))))
            with patch.dict(os.environ, {"PYTHONPATH": search_path}):
                ok, messages = doctor_runtime()
        self.assertFalse(ok)
        self.assertTrue(any("controlled broken scipy extension" in message for message in messages), messages)

    def test_diagnostics_capture_is_bounded_while_child_floods(self):
        command = [sys.executable, "-c", "import os,time; os.write(1, b'x'*2000000); time.sleep(60)"]
        with self.assertRaisesRegex(ValueError, "oversized"):
            _run_diagnostics(command, timeout=3)

    def test_diagnostics_timeout_reaps_a_stalled_child(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            _run_diagnostics([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.1)

    def test_unconfirmed_doctor_exit_is_retained_and_prevents_a_new_process(self):
        class DelayedProcess:
            returncode = 0
            stdout = io.BytesIO(b'{"ok":true,"messages":[]}\n')
            stderr = io.BytesIO()

            def poll(self):
                return self.returncode

        process = DelayedProcess()
        with patch.object(client, "_UNREAPED_DIAGNOSTICS", []), patch.object(
            client.subprocess, "Popen", return_value=process,
        ) as start, patch.object(client, "_terminate_and_reap", return_value=False):
            with self.assertRaises(DP5qWorkerUnavailable) as raised:
                _run_diagnostics(["fixture"])
            self.assertEqual(raised.exception.reason, "worker_cleanup_failed")
            self.assertEqual(client._UNREAPED_DIAGNOSTICS, [process])
            self.assertTrue(process.stdout.closed and process.stderr.closed)
            with self.assertRaisesRegex(DP5qWorkerUnavailable, "Previous diagnostic"):
                _run_diagnostics(["fixture"])
            start.assert_called_once()

    def test_doctor_thread_start_failure_reaps_the_created_process(self):
        original_start = subprocess.Popen
        processes = []
        def start(*args, **kwargs):
            process = original_start(*args, **kwargs)
            processes.append(process)
            return process
        with patch("checksi.dp5q.client.check_availability", return_value={"model_id": "fixture"}), \
             patch("checksi.dp5q.client.subprocess.Popen", side_effect=start), \
             patch.object(threading.Thread, "start", side_effect=RuntimeError("controlled thread creation failure")):
            ok, messages = doctor_runtime()
        self.assertFalse(ok)
        self.assertIn("controlled thread creation failure", "\n".join(messages))
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())

    def test_worker_thread_limits_are_explicit(self):
        with patch.dict("os.environ", {"OMP_NUM_THREADS": "99"}):
            env = worker_environment()
        self.assertEqual(env["OMP_NUM_THREADS"], "1")

    def test_client_import_keeps_numerical_libraries_out_of_parent(self):
        code = "import sys; from checksi.dp5q.client import DP5qSession; assert not any(name in sys.modules for name in ('numpy','scipy','rdkit'))"
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_actual_doctor_loads_bundled_models_and_predicts(self):
        ok, messages = doctor_runtime()
        self.assertTrue(ok, "\n".join(messages))
        self.assertTrue(any("finite mean/quantile predictions" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
