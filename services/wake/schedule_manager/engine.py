# meiva:note
# Execute one declarative payload with the argv of a direct Python script invocation.

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def run_single_round_payload(payload_path: Path) -> None:
    runner_argv = sys.argv[:]
    sys.argv[:] = [str(payload_path)]
    try:
        runpy.run_path(str(payload_path), run_name="__main__")
    finally:
        sys.argv[:] = runner_argv
