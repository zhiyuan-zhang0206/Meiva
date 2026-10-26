from __future__ import annotations

import os
import sys
from pathlib import Path

from base.host.proc import run_bounded

_HOST_POSTURE = Path(__file__).with_name("child_sdk_host_posture.py")


def _run_host_posture(scenario: str, tmp_path: Path) -> None:
    result = run_bounded(
        [sys.executable, "-I", str(_HOST_POSTURE), scenario],
        env={**os.environ, "MEIVA_HOME": str(tmp_path), "MEIVA_CONFIG_FETCH": "skip"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_host_startup_refuses_env_context_and_attachment(tmp_path: Path) -> None:
    _run_host_posture("host_refuses_env_context_and_attachment", tmp_path)


def test_executable_host_rejects_inherited_child_identity_before_sdk_import(tmp_path: Path) -> None:
    _run_host_posture("host_rejects_inherited_child_identity", tmp_path)
