"""Shell sessions are an agent's: a process without an agent identity is refused, never given a global."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_PROBE = Path(__file__).with_name("child_sessions_probe.py")


def _run_probe(home: Path, *, agent_id: int | None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "MEIVA_AGENT_ID"}
    env.update(MEIVA_HOME=str(home), MEIVA_CONFIG_FETCH="skip")
    if agent_id is not None:
        env["MEIVA_AGENT_ID"] = str(agent_id)
    result = subprocess.run(  # noqa: S603 — fixed interpreter and probe script
        [sys.executable, str(_PROBE)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


def test_exec_child_without_identity_is_refused_before_any_backend_or_database(
    tmp_path: Path,
) -> None:
    line = _run_probe(tmp_path, agent_id=None)
    assert line.startswith("REFUSED=Cannot use shell sessions: this process has no agent identity")


def test_exec_child_with_the_identity_the_launcher_passes_reaches_the_backend(
    tmp_path: Path,
) -> None:
    assert _run_probe(tmp_path, agent_id=41) == "LIST={}"
