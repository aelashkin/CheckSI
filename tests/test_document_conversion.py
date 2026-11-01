"""Headless DOCX conversion with real documents and controlled process boundaries."""

from __future__ import annotations

from contextlib import chdir, closing
import os
import io
import json
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import fitz
from docx import Document

from checksi.utils.platform_helpers import ConversionCancelled, convert_docx_to_pdf


class ConverterProcess:
    """Represent a converter process without invoking any installed office suite."""

    pid = 123456789

    def __init__(self, returncode: int | None = 0):
        self.exit_status = returncode
        self.returncode = None
        self.killed = False

    def poll(self):
        self.returncode = self.exit_status
        return self.returncode

    def kill(self):
        self.killed = True
        self.exit_status = -9

    def wait(self, timeout=None):
        self.returncode = -9 if self.exit_status is None else self.exit_status
        return self.returncode


class SupervisorProcess:
    """Represent only the supervisor OS boundary, including its binary terminal."""

    def __init__(self, payload):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(payload if isinstance(payload, bytes) else json.dumps(payload).encode() + b"\n")
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9

class DocumentConversionTests(unittest.TestCase):
    """Keep sources untouched and trust only a validated PDF from this attempt."""

    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.source_dir = self.directory / "source.docx dir Ω"
        self.source_dir.mkdir()
        self.source = self.source_dir / "Résumé compound Ω.DOCX"
        document = Document()
        document.add_paragraph("Conversion fixture Ω")
        document.save(self.source)
        self.original = self.source.read_bytes()
        self.work = self.directory / "run work Ω"
        self.work.mkdir()
        executable_name = "soffice.exe" if os.name == "nt" else "soffice"
        self.executable = self.directory / "Libre Office" / executable_name
        self.executable.parent.mkdir()
        self.executable.write_text("controlled executable", encoding="utf-8")
        self.executable.chmod(0o700)
        self.messages: list[str] = []
        self.commands = []
        self.process = ConverterProcess()
        self.cancel_token = None
        for name, default in (("P_PID", 1), ("WEXITED", 2), ("WNOHANG", 1), ("WNOWAIT", 0x1000000)):
            self.enterContext(patch.object(os, name, getattr(os, name, default), create=True))
        self.enterContext(patch("checksi.utils.document_conversion.os.waitid", create=True,
                                side_effect=lambda *args: object() if self.process.exit_status is not None else None))
        self.spawn = Mock(side_effect=self.create_pdf)
        self.supervisor_spawn = self.enterContext(patch(
            "checksi.utils.document_conversion.subprocess.Popen", side_effect=self.create_supervisor
        ))

    def create_supervisor(self, command, **kwargs):
        from checksi.utils import conversion_supervisor as supervisor

        request = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        warnings = []
        try:
            supervisor._stage_input(request, threading.Event())
            process = supervisor._launch_converter(request["command"], Path(request["attempt"]), _popen=self.spawn)
            supervisor._wait_for_conversion(process, max(0, request["deadline"] - time.monotonic()),
                                            self.cancel_token, warnings.append)
            result = {"status": "ok", "warnings": warnings}
        except ConversionCancelled:
            result = {"status": "cancelled", "warnings": warnings}
        except OSError as exc:
            result = {"status": "failed", "message": f"LibreOffice could not start ({exc}). "
                      "Check conversion.libreoffice_path and executable permissions.", "warnings": warnings}
        except RuntimeError as exc:
            result = {"status": "failed", "message": str(exc), "warnings": warnings}
        self.last_supervisor = SupervisorProcess(result)
        return self.last_supervisor

    def create_pdf(self, command, **kwargs):
        self.commands.append((command, kwargs))
        copied_source = Path(command[-1])
        self.assertEqual(Document(copied_source).paragraphs[0].text, "Conversion fixture Ω")
        output = Path(command[command.index("--outdir") + 1]) / copied_source.with_suffix(".pdf").name
        with fitz.open() as document:
            document.new_page().insert_text((20, 20), "Conversion fixture")
            document.save(output)
        return self.process

    def convert(self, **kwargs):
        self.cancel_token = kwargs.get("cancel_token")
        return convert_docx_to_pdf(
            self.source, self.messages.append, work_dir=self.work,
            executable=self.executable, **kwargs,
        )

    def assert_failed_cleanly(self) -> None:
        self.assertEqual(list(self.work.iterdir()), [])
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertIn("PDF highlighting", " ".join(self.messages))

    def test_unicode_read_only_uppercase_source_converts_without_touching_siblings(self) -> None:
        sibling = self.source.with_suffix(".pdf")
        sibling.write_bytes(b"pre-existing sibling must be preserved")
        self.source.chmod(0o400)
        self.source_dir.chmod(0o500)
        try:
            result = self.convert()
        finally:
            self.source_dir.chmod(0o700)
            self.source.chmod(0o600)
        self.assertIsInstance(result, Path)
        self.assertTrue(result.is_relative_to(self.work))
        with fitz.open(result) as document:
            self.assertEqual(document.page_count, 1)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(sibling.read_bytes(), b"pre-existing sibling must be preserved")
        self.assertEqual(set(self.source_dir.iterdir()), {self.source, sibling})

    def test_each_attempt_uses_private_copy_profile_and_output(self) -> None:
        first = self.convert()
        second = self.convert()
        self.assertNotEqual(first, second)
        profiles = []
        for command, kwargs in self.commands:
            copied = Path(command[-1])
            self.assertEqual(copied.name, "input.docx")
            self.assertTrue(copied.is_relative_to(self.work))
            self.assertEqual(kwargs["cwd"], copied.parent)
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            self.assertNotIn("shell", kwargs)
            profile = next(item for item in command if item.startswith("-env:UserInstallation="))
            self.assertEqual(profile, "-env:UserInstallation=" + (copied.parent / "profile").as_uri())
            self.assertIn("%20", profile)
            profiles.append(profile)
            if os.name == "posix":
                self.assertEqual(copied.parent.stat().st_mode & 0o777, 0o700)
                self.assertTrue(kwargs["start_new_session"])
        self.assertNotEqual(*profiles)

    def test_missing_installation_warns_and_creates_no_work(self) -> None:
        with patch("checksi.utils.document_conversion.sys.platform", "linux"), patch(
            "checksi.utils.document_conversion.shutil.which", return_value=None
        ):
            result = convert_docx_to_pdf(self.source, self.messages.append, work_dir=self.work)
        self.assertIsNone(result)
        self.spawn.assert_not_called()
        self.assertIn("conversion.libreoffice_path", " ".join(self.messages))
        self.assert_failed_cleanly()

    def test_explicit_missing_executable_warns_without_using_a_different_installation(self) -> None:
        result = convert_docx_to_pdf(
            self.source, self.messages.append, work_dir=self.work,
            executable=self.directory / "missing soffice",
        )
        self.assertIsNone(result)
        self.spawn.assert_not_called()
        self.assertIn("conversion.libreoffice_path", " ".join(self.messages))
        self.assert_failed_cleanly()

    def test_path_discovery_finds_soffice_before_libreoffice(self) -> None:
        with patch.dict(os.environ, {"PATH": str(self.executable.parent), "ProgramFiles": "",
                                     "ProgramFiles(x86)": "", "LOCALAPPDATA": ""}):
            result = convert_docx_to_pdf(self.source, work_dir=self.work)
        self.assertIsNotNone(result)
        self.assertEqual(self.commands[0][0][0], str(self.executable))

    def test_nonzero_exit_rejects_even_a_readable_generated_pdf(self) -> None:
        self.process.exit_status = 7
        self.assertIsNone(self.convert())
        self.assertIn("exit status 7", " ".join(self.messages))
        self.assert_failed_cleanly()

    def test_success_exit_without_pdf_cannot_reuse_preexisting_sibling(self) -> None:
        sibling = self.source.with_suffix(".pdf")
        with fitz.open() as document:
            document.new_page()
            document.save(sibling)
        self.spawn.side_effect = None
        self.spawn.return_value = self.process
        self.assertIsNone(self.convert())
        self.assertTrue(sibling.is_file())
        self.assertIn("did not create", " ".join(self.messages))
        self.assert_failed_cleanly()

    def test_invalid_or_empty_output_is_rejected(self) -> None:
        for content in (b"", b"not a PDF", self.empty_pdf()):
            with self.subTest(content=content[:20]):
                def invalid_output(command, **kwargs):
                    output = Path(command[command.index("--outdir") + 1]) / "input.pdf"
                    output.write_bytes(content)
                    return self.process
                self.spawn.side_effect = invalid_output
                self.assertIsNone(self.convert())
                self.assert_failed_cleanly()

    @staticmethod
    def empty_pdf() -> bytes:
        """Build a valid PDF container whose page tree intentionally has no pages."""
        return (b"%PDF-1.4\n1 0 obj <</Type /Catalog /Pages 2 0 R>> endobj\n"
                b"2 0 obj <</Type /Pages /Count 0 /Kids []>> endobj\n"
                b"trailer <</Root 1 0 R>>\n%%EOF\n")

    def test_launch_failure_is_actionable_and_cleans_copy_and_profile(self) -> None:
        self.spawn.side_effect = PermissionError("executable is not permitted")
        self.assertIsNone(self.convert())
        self.assertIn("could not start", " ".join(self.messages))
        self.assert_failed_cleanly()

    def test_encrypted_pdf_is_rejected(self) -> None:
        def encrypted_output(command, **kwargs):
            output = Path(command[command.index("--outdir") + 1]) / "input.pdf"
            with fitz.open() as document:
                document.new_page()
                document.save(output, encryption=fitz.PDF_ENCRYPT_AES_256,
                              owner_pw="owner fixture", user_pw="user fixture")
            return self.process
        self.spawn.side_effect = encrypted_output
        self.assertIsNone(self.convert())
        self.assert_failed_cleanly()

    def test_windows_cancellation_targets_only_the_owned_pid_tree(self) -> None:
        token = threading.Event()
        self.process.exit_status = None
        create_pdf = self.spawn.side_effect
        def cancel_after_launch(command, **kwargs):
            result = create_pdf(command, **kwargs)
            token.set()
            return result
        self.spawn.side_effect = cancel_after_launch
        with (
            patch("checksi.utils.document_conversion.sys.platform", "win32"),
            patch("checksi.utils.document_conversion.shutil.which", return_value=str(self.executable)),
            patch("subprocess.CREATE_NEW_PROCESS_GROUP", 0x200, create=True),
            patch("subprocess.CREATE_NO_WINDOW", 0x08000000, create=True),
            patch.dict(os.environ, {"SystemRoot": str(self.directory / "Windows")}),
            patch("checksi.utils.document_conversion.subprocess.run") as taskkill,
        ):
            with self.assertRaises(ConversionCancelled):
                self.convert(cancel_token=token)
        command = taskkill.call_args.args[0]
        self.assertEqual(command, [str(self.directory / "Windows/System32/taskkill.exe"),
                                   "/PID", str(self.process.pid), "/T", "/F"])
        self.assertEqual(taskkill.call_args.kwargs["timeout"], 2.0)
        self.assertEqual(self.commands[0][1]["creationflags"], 0x08000200)
        self.assert_failed_cleanly()

    def test_windows_conventional_location_prefers_console_launcher(self) -> None:
        from checksi.utils.document_conversion import _find_executable

        program_files = self.directory / "Program Files"
        console = program_files / "LibreOffice/program/soffice.com"
        console.parent.mkdir(parents=True)
        console.write_text("console launcher", encoding="utf-8")
        console.chmod(0o700)
        desktop = console.with_suffix(".exe")
        desktop.write_text("desktop launcher", encoding="utf-8")
        desktop.chmod(0o700)
        with (
            patch("checksi.utils.document_conversion.sys.platform", "win32"),
            patch("checksi.utils.document_conversion.shutil.which", return_value=None),
            patch.dict(os.environ, {"ProgramFiles": str(program_files),
                                   "ProgramFiles(x86)": "", "LOCALAPPDATA": ""}),
        ):
            self.assertEqual(_find_executable(None), console)

    def test_windows_discovery_does_not_trust_implicit_or_explicit_current_directory(self) -> None:
        from checksi.utils.document_conversion import _find_executable

        manuscript = self.directory / "untrusted submission"
        manuscript.mkdir()
        planted = manuscript / "soffice.com"
        planted.write_text("planted", encoding="utf-8")
        planted.chmod(0o700)
        trusted = self.directory / "trusted tools"
        trusted.mkdir()
        legitimate = trusted / "libreoffice.exe"
        legitimate.write_text("legitimate", encoding="utf-8")
        legitimate.chmod(0o700)
        batch = trusted / "soffice.cmd"
        batch.write_text("batch must not be discovered", encoding="utf-8")
        batch.chmod(0o700)
        search = os.pathsep.join(("", ".", str(manuscript), "relative tools", str(trusted)))
        environment = {"PATH": search, "PATHEXT": ".COM;.EXE;.BAT;.CMD",
                       "ProgramFiles": "", "ProgramFiles(x86)": "", "LOCALAPPDATA": ""}
        with (
            chdir(manuscript), patch.dict(os.environ, environment),
            patch("checksi.utils.document_conversion.sys.platform", "win32"),
            # Model Windows' unsafe which boundary; safe discovery must not call it.
            patch("checksi.utils.document_conversion.shutil.which", return_value=str(planted)),
        ):
            before = dict(os.environ)
            self.assertEqual(_find_executable(None), legitimate)
            self.assertEqual(dict(os.environ), before)

    def test_windows_program_files_precedes_ambient_path(self) -> None:
        from checksi.utils.document_conversion import _find_executable

        program_files = self.directory / "Program Files"
        installed = program_files / "LibreOffice/program/soffice.com"
        installed.parent.mkdir(parents=True)
        installed.write_text("installed", encoding="utf-8")
        installed.chmod(0o700)
        with (
            patch("checksi.utils.document_conversion.sys.platform", "win32"),
            patch("checksi.utils.document_conversion.shutil.which", return_value=str(self.executable)),
            patch.dict(os.environ, {"ProgramFiles": str(program_files),
                                   "ProgramFiles(x86)": "", "LOCALAPPDATA": ""}),
        ):
            self.assertEqual(_find_executable(None), installed)

    def test_windows_discovery_rejects_batch_files_but_explicit_path_remains_authoritative(self) -> None:
        from checksi.utils.document_conversion import _find_executable

        batch = self.directory / "soffice.cmd"
        batch.write_text("explicit command", encoding="utf-8")
        batch.chmod(0o700)
        with (
            patch("checksi.utils.document_conversion.sys.platform", "win32"),
            patch("checksi.utils.document_conversion.shutil.which", return_value=str(batch)),
            patch.dict(os.environ, {"PATH": str(self.directory), "ProgramFiles": "",
                                   "ProgramFiles(x86)": "", "LOCALAPPDATA": ""}),
        ):
            with self.assertRaisesRegex(RuntimeError, "LibreOffice was not found"):
                _find_executable(None)
            self.assertEqual(_find_executable(batch), batch)

    @unittest.skipUnless(os.name == "nt", "Native Windows current-directory search policy")
    def test_native_windows_planted_converter_cannot_override_trusted_path(self) -> None:
        from checksi.utils.document_conversion import _find_executable

        manuscript = self.directory / "native manuscript"
        manuscript.mkdir()
        trusted = self.directory / "native trusted tools"
        trusted.mkdir()
        for path in (manuscript / "soffice.com", manuscript / "soffice.bat",
                     manuscript / "libreoffice.exe", trusted / "libreoffice.exe"):
            path.write_text("fake executable boundary", encoding="utf-8")
            path.chmod(0o700)
        with chdir(manuscript), patch.dict(os.environ, {"PATH": str(trusted),
                "ProgramFiles": "", "ProgramFiles(x86)": "", "LOCALAPPDATA": ""}):
            self.assertEqual(_find_executable(None), trusted / "libreoffice.exe")

    def test_windows_process_exit_during_termination_preserves_cancellation(self) -> None:
        token = threading.Event()
        self.process.exit_status = None
        create_pdf = self.spawn.side_effect
        def cancel_after_launch(command, **kwargs):
            result = create_pdf(command, **kwargs)
            token.set()
            return result
        def already_exited():
            self.process.exit_status = 0
            raise ProcessLookupError("process exited during taskkill")
        self.spawn.side_effect = cancel_after_launch
        with (
            patch("checksi.utils.document_conversion.sys.platform", "win32"),
            patch("checksi.utils.document_conversion.shutil.which", return_value=str(self.executable)),
            patch("subprocess.CREATE_NEW_PROCESS_GROUP", 0x200, create=True),
            patch("subprocess.CREATE_NO_WINDOW", 0x08000000, create=True),
            patch.dict(os.environ, {"SystemRoot": str(self.directory / "Windows")}),
            patch("checksi.utils.document_conversion.subprocess.run",
                  side_effect=subprocess.CalledProcessError(128, "taskkill")),
            patch.object(self.process, "kill", side_effect=already_exited),
        ):
            with self.assertRaises(ConversionCancelled):
                self.convert(cancel_token=token)
        self.assert_failed_cleanly()

    @unittest.skipUnless(os.name == "posix", "POSIX owned process group")
    def test_timeout_kills_only_owned_group_and_cleans_partial_output(self) -> None:
        self.process.exit_status = None
        with patch("checksi.utils.document_conversion.os.killpg") as kill_group:
            self.assertIsNone(self.convert(timeout_seconds=0.1))
        kill_group.assert_called_once_with(self.process.pid, signal.SIGKILL)
        self.assertIn("conversion.timeout_seconds", " ".join(self.messages))
        self.assert_failed_cleanly()

    @unittest.skipUnless(os.name == "posix", "POSIX owned process group")
    def test_active_cancellation_raises_after_killing_owned_group_and_cleanup(self) -> None:
        token = threading.Event()
        self.process.exit_status = None
        create_pdf = self.spawn.side_effect
        def cancel_after_launch(command, **kwargs):
            result = create_pdf(command, **kwargs)
            token.set()
            return result
        self.spawn.side_effect = cancel_after_launch
        with patch("checksi.utils.document_conversion.os.killpg") as kill_group:
            with self.assertRaises(ConversionCancelled):
                self.convert(cancel_token=token)
        kill_group.assert_called_once_with(self.process.pid, signal.SIGKILL)
        self.assert_failed_cleanly()

    def test_pre_cancelled_attempt_never_starts_converter_or_creates_work(self) -> None:
        token = threading.Event()
        token.set()
        with self.assertRaises(ConversionCancelled):
            self.convert(cancel_token=token)
        self.spawn.assert_not_called()
        self.assert_failed_cleanly()

    def test_cancellation_wins_over_simultaneous_success(self) -> None:
        token = threading.Event()
        create_pdf = self.spawn.side_effect
        def finish_and_cancel(command, **kwargs):
            result = create_pdf(command, **kwargs)
            token.set()
            return result
        self.spawn.side_effect = finish_and_cancel
        with self.assertRaises(ConversionCancelled):
            self.convert(cancel_token=token)
        self.assert_failed_cleanly()

    def test_source_directory_cannot_be_used_as_conversion_workspace(self) -> None:
        result = convert_docx_to_pdf(
            self.source, self.messages.append, work_dir=self.source_dir,
            executable=self.executable,
        )
        self.assertIsNone(result)
        self.spawn.assert_not_called()
        self.assertEqual(list(self.source_dir.iterdir()), [self.source])

    def test_missing_source_or_work_directory_warns_without_starting(self) -> None:
        for source, work in ((self.source_dir / "absent.docx", self.work),
                             (self.source, self.directory / "absent work")):
            with self.subTest(source=source, work=work):
                result = convert_docx_to_pdf(
                    source, self.messages.append, work_dir=work, executable=self.executable,
                )
                self.assertIsNone(result)
                self.spawn.assert_not_called()
                self.assert_failed_cleanly()

    @unittest.skipUnless(os.name == "posix", "POSIX symlink creation")
    def test_docx_symlink_keeps_logical_suffix_when_target_is_extensionless(self) -> None:
        target = self.source.with_suffix("")
        self.source.rename(target)
        self.source.symlink_to(target.name)
        self.assertIsNotNone(self.convert())
        self.assertEqual(target.read_bytes(), self.original)

    def test_missing_home_does_not_break_optional_macos_discovery(self) -> None:
        from checksi.utils.document_conversion import _conventional_executables

        with (
            patch("checksi.utils.document_conversion.sys.platform", "darwin"),
            patch.object(Path, "home", side_effect=RuntimeError("home is unavailable")),
            patch("checksi.utils.document_conversion.shutil.which", return_value=None),
            patch("checksi.utils.document_conversion.os.access", return_value=False),
        ):
            self.assertEqual(_conventional_executables(), [Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")])
            result = convert_docx_to_pdf(self.source, self.messages.append, work_dir=self.work)
        self.assertIsNone(result)
        self.assert_failed_cleanly()

    def test_invalid_supervisor_terminals_are_optional_failures_with_owned_cleanup(self) -> None:
        for raw in (b"", b"not JSON\n", b"{}\n", b"x" * 16385,
                    b'{"status":"ok","warnings":[17]}\n'):
            with self.subTest(raw=raw[:32]):
                self.supervisor_spawn.side_effect = lambda *args, **kwargs: SupervisorProcess(raw)
                self.assertIsNone(self.convert())
                self.assert_failed_cleanly()

    def test_result_reader_start_failure_releases_owner_lease_and_removes_copy(self) -> None:
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("no reader thread")):
            self.assertIsNone(self.convert())
        self.assertTrue(self.last_supervisor.stdin.closed)
        self.assert_failed_cleanly()

    def test_success_lease_start_failure_remains_optional_and_removes_copy(self) -> None:
        start = threading.Thread.start
        def fail_lease(thread):
            if thread.name == "document-conversion-lease":
                raise RuntimeError("no lease thread")
            return start(thread)
        with patch.object(threading.Thread, "start", fail_lease):
            self.assertIsNone(self.convert())
        self.assertTrue(self.last_supervisor.stdin.closed)
        self.assert_failed_cleanly()


@unittest.skipUnless(os.name == "posix", "POSIX session and non-reaping child observation")
class ExitedLauncherTests(unittest.TestCase):
    """A launcher may exit while descendants still hold resources in its group."""

    def run_exited_launcher(self, *, returncode=0, cancelled=False) -> None:
        from checksi.utils.document_conversion import _wait_for_conversion

        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child.pid"
            launcher = (
                "import pathlib,subprocess,sys; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
                "sys.exit(int(sys.argv[2]))"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", launcher, str(pid_file), str(returncode)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True,
            )
            try:
                # Leave the leader waitable: production owns its PID until group teardown.
                deadline = time.monotonic() + 5
                if callable(getattr(os, "waitid", None)):
                    while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                        self.assertLess(time.monotonic(), deadline, "launcher did not exit")
                        time.sleep(0.01)
                else:
                    with closing(select.kqueue()) as observer:
                        event = select.kevent(process.pid, filter=select.KQ_FILTER_PROC,
                                              flags=select.KQ_EV_ADD, fflags=select.KQ_NOTE_EXIT)
                        observer.control([event], 0, 0)
                        self.assertTrue(observer.control(None, 1, 5), "launcher did not exit")
                self.assertTrue(pid_file.is_file())
                self.assertIsNone(process.returncode)
                # The child keeps stdout open. EOF proves exit even before an orphan is reaped.
                self.assertFalse(select.select([process.stdout], [], [], 0)[0])
                token = threading.Event()
                if cancelled:
                    token.set()
                if cancelled:
                    with self.assertRaises(ConversionCancelled):
                        _wait_for_conversion(process, 5, token, lambda message: None)
                elif returncode:
                    with self.assertRaisesRegex(RuntimeError, f"exit status {returncode}"):
                        _wait_for_conversion(process, 5, token, lambda message: None)
                else:
                    _wait_for_conversion(process, 5, token, lambda message: None)
                self.assertTrue(
                    select.select([process.stdout], [], [], 2)[0],
                    "a descendant survived the exited launcher and still holds stdout open",
                )
                self.assertEqual(process.stdout.read(), b"")
                self.assertEqual(process.returncode, returncode)
            finally:
                # A failing regression still owns a live child in this newly created group.
                if not select.select([process.stdout], [], [], 0)[0]:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
                process.stdout.close()

    def test_cancelled_exited_launcher_does_not_leave_its_child_alive(self) -> None:
        self.run_exited_launcher(cancelled=True)

    def test_failed_exited_launcher_does_not_leave_its_child_alive(self) -> None:
        self.run_exited_launcher(returncode=7)

    def test_successful_exited_launcher_does_not_leave_its_child_alive(self) -> None:
        self.run_exited_launcher()

    def test_successful_zombie_only_launcher_does_not_log_a_cleanup_failure(self) -> None:
        from checksi.utils.document_conversion import _wait_for_conversion

        messages = []
        process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        _wait_for_conversion(process, 5, None, messages.append)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(messages, [])

    @unittest.skipUnless(hasattr(select, "kqueue"), "macOS/BSD kqueue observation")
    def test_kqueue_fallback_preserves_exited_launcher_ownership(self) -> None:
        with patch.object(os, "waitid", None, create=True):
            self.run_exited_launcher(cancelled=True)


if __name__ == "__main__":
    unittest.main()
