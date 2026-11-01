"""Observe parent escalation on an owned test child without replacing signals."""

from contextlib import ExitStack
import os
import signal
from unittest.mock import patch


class ProcessEscalationObservation:
    """Record successful terminate/kill calls made while the child was live."""

    def __init__(self, process):
        self.process = process
        self.signals = []
        self.patches = ExitStack()

    def __enter__(self):
        for name in ("terminate", "kill"):
            original = getattr(self.process, name)

            def observed(*args, _name=name, _original=original, **kwargs):
                live = self.process.poll() is None
                result = _original(*args, **kwargs)
                if live:
                    self.signals.append(_name)
                return result

            self.patches.enter_context(patch.object(self.process, name, side_effect=observed))
        return self

    def __exit__(self, *args):
        return self.patches.__exit__(*args)

    def assert_cancelled_exit(self, test_case):
        """Require a reaped cooperative exit or its observed parent escalation."""
        returncode = self.process.poll()
        test_case.assertIsNotNone(returncode, "Cancellation left the child running.")
        allowed = {0}
        if os.name == "posix":
            for name, signum in (("terminate", signal.SIGTERM), ("kill", signal.SIGKILL)):
                if name in self.signals:
                    allowed.add(-signum)
        elif self.signals:
            # Popen terminate/kill both use TerminateProcess(handle, 1).
            allowed.add(1)
        test_case.assertIn(returncode, allowed, f"Observed parent escalation: {self.signals}")
