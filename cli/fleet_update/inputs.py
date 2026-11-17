# meiva:note
# Ambient inputs captured by the CLI entry for one update window.


from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from base.paths.scratch import ScratchSpace

from .session import Pacing, Timers


@dataclass(frozen=True)
class UpdateWindowInputs:
    repo: Path
    env: Mapping[str, str]
    now: datetime
    pacing: Pacing
    timers: Timers
    operation_id: str
    initiator: str
    python: str
    # meiva:note
    # The temporary space a validation mints its throwaway `MEIVA_HOME` in: the roster check reads
    # this checkout's services through a home no process can reach, so the entry hands in the space
    # rather than the module naming the host's.
    scratch: ScratchSpace
