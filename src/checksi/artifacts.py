"""Atomic run-directory allocation, independent of input-directory permissions."""
from __future__ import annotations

from pathlib import Path
import tempfile


def reserve_run_directory(output_dir: str | Path, input_path: str | Path) -> Path:
    """Reserve one private directory atomically; never reuse a previous run."""
    parent = Path(output_dir).expanduser().absolute()
    parent.mkdir(parents=True, exist_ok=True)
    stem = ''.join(c if c.isalnum() or c in '-_ ' else '_' for c in Path(input_path).stem)[:32] or 'input'
    return Path(tempfile.mkdtemp(prefix=f'{stem}-', dir=parent))


def reserve_file(destination: str | Path) -> Path:
    """Atomically reserve an artifact name without clobbering existing files."""
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    counter = 0
    while True:
        candidate = target if not counter else target.with_name(f'{target.stem}_{counter}{target.suffix}')
        try:
            with candidate.open('xb'):
                pass
            return candidate
        except FileExistsError:
            counter += 1
