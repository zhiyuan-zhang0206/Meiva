import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from base.host.proc import run_bounded


def run_boundary(case: str, *, home: Path, env: Mapping[str, str], args: Sequence[str]) -> None:
    result = run_bounded(
        [
            sys.executable,
            "-I",
            "-m",
            "base.packages.plugins.tests.plugin_boundary_child",
            case,
            *args,
        ],
        env={
            "PATH": os.defpath,
            "MEIVA_HOME": str(home),
            "MEIVA_CONFIG_FETCH": "skip",
            **env,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
