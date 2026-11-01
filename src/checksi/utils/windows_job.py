"""Win32 process ownership for the private converter supervisor (Windows 10+)."""

from __future__ import annotations

import ctypes as ct
import os
from pathlib import Path
import subprocess
import time


HANDLE = ct.c_void_p
DWORD = ct.c_uint32
SIZE_T = ct.c_size_t


class _BasicLimits(ct.Structure):
    _fields_ = [("process_time", ct.c_int64), ("job_time", ct.c_int64), ("flags", DWORD),
                ("minimum_working_set", SIZE_T), ("maximum_working_set", SIZE_T),
                ("active_process_limit", DWORD), ("affinity", SIZE_T),
                ("priority", DWORD), ("scheduling_class", DWORD)]


class _ExtendedLimits(ct.Structure):
    _fields_ = [("basic", _BasicLimits), ("io_counters", ct.c_uint64 * 6),
                ("process_memory", SIZE_T), ("job_memory", SIZE_T),
                ("peak_process_memory", SIZE_T), ("peak_job_memory", SIZE_T)]


class _StartupInfo(ct.Structure):
    _fields_ = [("cb", DWORD), ("reserved", ct.c_wchar_p), ("desktop", ct.c_wchar_p),
                ("title", ct.c_wchar_p), ("x", DWORD), ("y", DWORD), ("x_size", DWORD),
                ("y_size", DWORD), ("x_chars", DWORD), ("y_chars", DWORD),
                ("fill", DWORD), ("flags", DWORD), ("show", ct.c_uint16),
                ("reserved_size", ct.c_uint16), ("reserved_bytes", ct.c_void_p),
                ("stdin", HANDLE), ("stdout", HANDLE), ("stderr", HANDLE)]


class _StartupInfoEx(ct.Structure):
    _fields_ = [("startup", _StartupInfo), ("attributes", ct.c_void_p)]


class _ProcessInfo(ct.Structure):
    _fields_ = [("process", HANDLE), ("thread", HANDLE), ("pid", DWORD), ("thread_id", DWORD)]


class _Accounting(ct.Structure):
    _fields_ = [("times", ct.c_int64 * 4), ("page_faults", DWORD), ("total", DWORD),
                ("active", DWORD), ("terminated", DWORD)]


class WindowsJobProcess:
    """Create a child atomically inside a non-inheritable kill-on-close Job Object.

    PROC_THREAD_ATTRIBUTE_JOB_LIST associates the job before any child code can
    run. HANDLE_LIST passes only a private NUL handle, never the owner's pipe or
    the job handle. A launcher exit therefore cannot release descendant ownership.
    """

    def __init__(self, command: list[str], *, cwd: Path) -> None:
        import msvcrt

        self.command = command
        self.returncode = None
        self._kernel = ct.WinDLL("kernel32", use_last_error=True)
        self._job = self._process = None
        self._declare_api()
        attributes = None
        initialized = False
        try:
            self._job = self._kernel.CreateJobObjectW(None, None)
            self._require(self._job)
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            self._require(self._kernel.SetInformationJobObject(self._job, 9, ct.byref(limits), ct.sizeof(limits)))
            size = SIZE_T()
            self._kernel.InitializeProcThreadAttributeList(None, 2, 0, ct.byref(size))
            if not 0 < size.value <= 65536:
                raise OSError("Windows could not size the converter process attributes")
            attributes = ct.create_string_buffer(size.value)
            self._require(self._kernel.InitializeProcThreadAttributeList(attributes, 2, 0, ct.byref(size)))
            initialized = True
            jobs = (HANDLE * 1)(self._job)
            self._require(self._kernel.UpdateProcThreadAttribute(
                attributes, 0, 0x2000D, jobs, ct.sizeof(jobs), None, None))  # JOB_LIST
            with open(os.devnull, "r+b", buffering=0) as null:
                null_handle = msvcrt.get_osfhandle(null.fileno())
                os.set_handle_inheritable(null_handle, True)
                handles = (HANDLE * 1)(null_handle)
                self._require(self._kernel.UpdateProcThreadAttribute(
                    attributes, 0, 0x20002, handles, ct.sizeof(handles), None, None))  # HANDLE_LIST
                startup = _StartupInfoEx()
                startup.startup.cb = ct.sizeof(startup)
                startup.startup.flags = 0x100  # STARTF_USESTDHANDLES
                startup.startup.stdin = startup.startup.stdout = startup.startup.stderr = null_handle
                startup.attributes = ct.cast(attributes, ct.c_void_p)
                info = _ProcessInfo()
                arguments = ct.create_unicode_buffer(subprocess.list2cmdline(command))
                # No shell, no console attachment, and the job is assigned atomically.
                flags = 0x80000 | 0x08000000 | 0x200
                self._require(self._kernel.CreateProcessW(
                    command[0], arguments, None, None, True, flags, None, str(cwd),
                    ct.byref(startup), ct.byref(info)))
            self._process = info.process
            self.pid = info.pid
            self._kernel.CloseHandle(info.thread)
        except BaseException:
            self.close()
            raise
        finally:
            if initialized:
                self._kernel.DeleteProcThreadAttributeList(attributes)

    def _declare_api(self) -> None:
        """Use pointer-sized handles and fixed Win32 integer widths."""
        signatures = {
            "CreateJobObjectW": ((ct.c_void_p, ct.c_wchar_p), HANDLE),
            "SetInformationJobObject": ((HANDLE, ct.c_int, ct.c_void_p, DWORD), ct.c_int),
            "InitializeProcThreadAttributeList": ((ct.c_void_p, DWORD, DWORD, ct.POINTER(SIZE_T)), ct.c_int),
            "UpdateProcThreadAttribute": ((ct.c_void_p, DWORD, SIZE_T, ct.c_void_p, SIZE_T,
                                           ct.c_void_p, ct.c_void_p), ct.c_int),
            "DeleteProcThreadAttributeList": ((ct.c_void_p,), None),
            "CreateProcessW": ((ct.c_wchar_p, ct.c_wchar_p, ct.c_void_p, ct.c_void_p, ct.c_int,
                                 DWORD, ct.c_void_p, ct.c_wchar_p, ct.c_void_p, ct.POINTER(_ProcessInfo)), ct.c_int),
            "WaitForSingleObject": ((HANDLE, DWORD), DWORD),
            "GetExitCodeProcess": ((HANDLE, ct.POINTER(DWORD)), ct.c_int),
            "TerminateJobObject": ((HANDLE, ct.c_uint), ct.c_int),
            "QueryInformationJobObject": ((HANDLE, ct.c_int, ct.c_void_p, DWORD, ct.c_void_p), ct.c_int),
            "TerminateProcess": ((HANDLE, ct.c_uint), ct.c_int),
            "CloseHandle": ((HANDLE,), ct.c_int),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self._kernel, name)
            function.argtypes, function.restype = arguments, result

    @staticmethod
    def _require(value) -> None:
        if not value:
            raise ct.WinError(ct.get_last_error())

    def poll(self) -> int | None:
        """Read exit status through the retained process handle, never a recycled PID."""
        status = self._kernel.WaitForSingleObject(self._process, 0)
        if status == 0x102:
            return None
        if status != 0:
            raise OSError(f"Windows converter wait failed with status {status}")
        result = DWORD()
        self._require(self._kernel.GetExitCodeProcess(self._process, ct.byref(result)))
        self.returncode = result.value
        return self.returncode

    def wait(self, timeout: float) -> int:
        """Keep process waits bounded after job termination."""
        deadline = time.monotonic() + timeout
        while self.poll() is None:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.command, timeout)
            time.sleep(0.01)
        return self.returncode

    def kill(self) -> None:
        self._require(self._kernel.TerminateProcess(self._process, 1))

    def stop_tree(self) -> None:
        """Terminate descendants even if the original launcher already exited."""
        self._require(self._kernel.TerminateJobObject(self._job, 1))
        deadline = time.monotonic() + 2
        while True:
            accounting = _Accounting()
            self._require(self._kernel.QueryInformationJobObject(
                self._job, 1, ct.byref(accounting), ct.sizeof(accounting), None))
            if not accounting.active:
                return
            if time.monotonic() >= deadline:
                raise OSError("Windows converter job still contains active processes after termination")
            time.sleep(0.01)

    def close(self) -> None:
        """Closing the last job handle also kills children after supervisor death."""
        for name in ("_job", "_process"):
            handle = getattr(self, name)
            if handle:
                self._kernel.CloseHandle(handle)
                setattr(self, name, None)
