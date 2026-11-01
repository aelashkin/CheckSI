#!/usr/bin/env python3
"""Project setup helper with an optional bundled DP5q self-check."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"


def _run(cmd: list[str]) -> None:
    print("Running: " + " ".join(cmd))
    subprocess.run(cmd, cwd=REPO_ROOT, check=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install/sync CheckSI and optionally check its bundled DP5q model.",
    )
    parser.add_argument(
        "--with-dp5",
        action="store_true",
        help="Run the bundled DP5q model self-check after syncing CheckSI.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        _run(["uv", "sync", "--locked"])
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"Setup failed while syncing CheckSI: {exc}", file=sys.stderr)
        return 1

    if not args.with_dp5:
        print("CheckSI setup complete. Bundled DP5q self-check was not requested.")
        return 0

    sys.path.insert(0, str(SRC_ROOT))
    try:
        from checksi.dp5q.client import DP5qAvailabilityError, doctor_runtime

        ok, messages = doctor_runtime()
    except DP5qAvailabilityError as exc:
        print(f"DP5q self-check failed: {exc}", file=sys.stderr)
        return 1

    for message in messages:
        print(message)
    if not ok:
        print("DP5q self-check failed. Repair or reinstall the CheckSI package.", file=sys.stderr)
        return 1
    print("CheckSI setup complete; bundled DP5q self-check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
