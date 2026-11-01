"""Real processes prove private conversion work cannot outlive its owner."""

from __future__ import annotations

import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from docx import Document
import fitz


def wait_until(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


@unittest.skipUnless(os.name == "posix", "POSIX owner SIGKILL and FIFO lifetime proof")
class ConversionOwnerLifetimeTests(unittest.TestCase):
    """Use a real external converter fixture, never a live office installation."""

    def check_owner_death(self, *, completed=False, stopped_owner=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "manuscript Ω.DOCX"
            Document().save(source)
            work = root / "run work"
            work.mkdir()
            reference = root / "reference.pdf"
            with fitz.open() as pdf:
                pdf.new_page().insert_text((20, 20), "owned conversion fixture")
                pdf.save(reference)
            fifo = root / "converter-lifetime.fifo"
            os.mkfifo(fifo)
            read_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
            executable = root / "soffice"
            executable.write_text(
                "#!" + sys.executable + "\n"
                "import json,os,pathlib,shutil,subprocess,sys,time\n"
                "attempt=pathlib.Path(sys.argv[-1]).parent\n"
                "hold=os.open(os.environ['CONVERTER_TEST_FIFO'],os.O_WRONLY)\n"
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],pass_fds=(hold,))\n"
                "marker=attempt/'pids.tmp'\n"
                "marker.write_text(json.dumps({'launcher':os.getpid(),'child':child.pid,'group':os.getpgrp()}))\n"
                "marker.replace(attempt/'pids.json')\n"
                "if os.environ.get('CONVERTER_TEST_SUCCESS')=='1':\n"
                " out=pathlib.Path(sys.argv[sys.argv.index('--outdir')+1])/'input.pdf'\n"
                " shutil.copyfile(os.environ['CONVERTER_TEST_PDF'],out)\n"
                " sys.exit(0)\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            owner_code = (
                "import pathlib,sys,time; "
                "from checksi.utils.platform_helpers import convert_docx_to_pdf; "
                "result=convert_docx_to_pdf(sys.argv[1],lambda message:None,work_dir=sys.argv[2],"
                "executable=sys.argv[3],timeout_seconds=float(sys.argv[4])); "
                "print('READY',str(result),flush=True); time.sleep(60)"
            )
            environment = dict(os.environ, CONVERTER_TEST_FIFO=str(fifo),
                               CONVERTER_TEST_PDF=str(reference),
                               CONVERTER_TEST_SUCCESS="1" if completed else "0")
            owner = subprocess.Popen([sys.executable, "-u", "-c", owner_code,
                                      str(source), str(work), str(executable), "1.5" if stopped_owner else "30"],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=environment)
            pids = None
            descendants_finished = False
            try:
                self.assertTrue(wait_until(lambda: list(work.glob("docx-pdf-*/pids.json"))),
                                "external converter did not start")
                marker = next(work.glob("docx-pdf-*/pids.json"))
                pids = json.loads(marker.read_text(encoding="utf-8"))
                attempt = marker.parent
                if completed:
                    self.assertTrue(select.select([owner.stdout], [], [], 6)[0], "conversion never returned")
                    self.assertTrue(owner.stdout.readline().startswith(b"READY "))
                    self.assertTrue((attempt / "output/input.pdf").is_file())
                else:
                    self.assertFalse(select.select([read_fd], [], [], 0)[0], "fixture did not hold its descriptor")
                self.assertTrue((attempt / "input.docx").is_file())
                if stopped_owner:
                    os.kill(owner.pid, signal.SIGSTOP)
                else:
                    owner.kill()
                    owner.wait(timeout=2)
                self.assertTrue(select.select([read_fd], [], [], 4)[0],
                                "converter descendants survived SIGKILL of their owner")
                self.assertEqual(os.read(read_fd, 1), b"")
                descendants_finished = True
                self.assertTrue(wait_until(lambda: not attempt.exists(), timeout=4),
                                "private manuscript copy survived SIGKILL of its owner")
                self.assertEqual(list(work.iterdir()), [])
                if stopped_owner:
                    self.assertIsNone(owner.poll(), "the deadline must work while its live owner is suspended")
            finally:
                if stopped_owner and owner.poll() is None:
                    os.kill(owner.pid, signal.SIGCONT)
                if owner.poll() is None:
                    owner.kill()
                owner.wait(timeout=2)
                if pids and not descendants_finished:
                    # The still-open FIFO proves this freshly created group remains owned.
                    try:
                        os.killpg(pids["group"], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                os.close(read_fd)
                owner.stdout.close()
                owner.stderr.close()

    def test_sigkill_owner_stops_running_converter_descendants_and_removes_private_copy(self):
        self.check_owner_death()

    def test_sigkill_after_success_still_removes_run_owned_private_copy(self):
        self.check_owner_death(completed=True)

    def test_deadline_still_stops_descendants_while_live_owner_is_suspended(self):
        self.check_owner_death(stopped_owner=True)


if __name__ == "__main__":
    unittest.main()
