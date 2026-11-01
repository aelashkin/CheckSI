"""Inject endpoint faults while retaining actual model loading/calculation."""

import builtins
import sys

from checksi.dp5q import worker

mode = sys.argv.pop(1)
serve = worker._serve


def fault_serve(*args):
    if mode in ("initialization", "blocked_import"):
        original_import = builtins.__import__
        def fail_import(name, *values, **kwargs):
            if name == "network" and mode == "blocked_import":
                import time
                print("BLOCKED-IMPORT", file=sys.stderr, flush=True)
                time.sleep(60)
            if name == "engine":
                raise ImportError("controlled engine initialization failure")
            return original_import(name, *values, **kwargs)
        builtins.__import__ = fail_import
    else:
        from checksi.dp5q import engine
        original_check = engine.check_compound
        calls = 0
        def fail_first(*values, **kwargs):
            nonlocal calls
            calls += 1
            if mode == "native_sigint_complete":
                import signal
                import time
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                kwargs["progress"]("geometry")
                time.sleep(.25)
                return original_check(*values, **kwargs)
            if mode in ("native_block", "native_sigint"):
                import time
                if mode == "native_sigint":
                    import signal
                    signal.signal(signal.SIGINT, signal.SIG_DFL)
                kwargs["progress"]("geometry")
                time.sleep(60)
            if calls == 1:
                raise RuntimeError("controlled unexpected calculation failure")
            return original_check(*values, **kwargs)
        engine.check_compound = fail_first
    return serve(*args)


worker._serve = fault_serve
raise SystemExit(worker.main())
