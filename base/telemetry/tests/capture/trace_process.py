import sys
from pathlib import Path

from base.host.proc import run_bounded


def run_trace_scenario(home: Path, scenario: str, *, lifecycle: bool = False) -> None:
    env = {
        "HOME": str(home),
        "MEIVA_HOME": str(home),
        "MEIVA_CONFIG_FETCH": "skip",
        "MEIVA_CONFIG_BOOT": "eager",
        "MEIVA_PROCESS_PROFILE": "agent",
    }
    if lifecycle:
        result = run_bounded(
            [
                sys.executable,
                "-I",
                str(Path(__file__).with_name("trace_lifecycle_child.py")),
                scenario,
            ],
            env=env,
            timeout=90,
            capture_output=True,
            text=True,
        )
    else:
        result = run_bounded(
            [
                sys.executable,
                "-I",
                str(Path(__file__).with_name("trace_facade_child.py")),
                scenario,
            ],
            env=env,
            timeout=90,
            capture_output=True,
            text=True,
        )
    assert result.returncode == 0, result.stdout + result.stderr
