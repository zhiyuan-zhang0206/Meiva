import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path


def run_boundary(case: str, *, home: Path, env: Mapping[str, str], args: Sequence[str]) -> None:
    result = subprocess.run(
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
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
