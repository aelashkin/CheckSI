"""Support ``python -m checksi`` with the installed CLI contract."""

from checksi.main import entrypoint

if __name__ == "__main__":
    raise SystemExit(entrypoint())
