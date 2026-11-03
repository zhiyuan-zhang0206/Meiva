# meiva:note
# The flags `admission-run` reads, as the parser answers them: the floor's own flags
# (`admission_floor.FloorOptions`) plus the job's concurrency, memory and wait flags and the
# command after `--`.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scripts.host_ops.admission_floor import FloorOptions

__all__ = ["RunOptions"]


@dataclass(frozen=True)
class RunOptions(FloorOptions):
    peak_mb: float | None
    jobs: int | None
    reserve_gib: float
    swap_multiplier: float
    no_admission: str | None
    max_wait: float
    no_wait: bool
    dry_run: bool
    home: Path | None
    command: list[str]
