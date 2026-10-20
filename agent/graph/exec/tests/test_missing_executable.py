import asyncio
from pathlib import Path

import pytest

from agent.graph.exec import _subprocess
from meiva.tests.fixtures.pin_agent import exec_context


async def test_real_missing_executable_crashes_the_exec_without_a_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agent.graph.exec._result import ExecCrashed
    from agent.graph.exec._subprocess import run_in_subprocess

    monkeypatch.setattr(_subprocess.sys, "executable", str(tmp_path / "absent"))
    outcome, _ = await run_in_subprocess(
        "raise AssertionError('must never execute')",
        exec_context(None),
        asyncio.Event(),
        2,
        exec_dir=tmp_path,
        accumulation_max_chars=1_000_000,
    )
    assert isinstance(outcome, ExecCrashed)
    assert "could not be spawned" in outcome.output
