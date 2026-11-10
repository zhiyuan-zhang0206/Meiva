# meiva:note
# The memory arbiter's two daily jobs, woken through a gateway on a loopback port.

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from base.agents.context import MeivaContext
from meiva.gateway_client import use_client
from schedules.jobs.memory_maintenance import wake_arbiter
from schedules.tests.support.gateway_directory import AgentRowSpec, DirectoryPage, serve_gateway

_SLOT = datetime(2026, 9, 7, 4, 0, tzinfo=UTC)
_JOB = "Daily consolidation (commit + push + PR merge)"
_ARBITER = (
    DirectoryPage("memory-arbiter", None, (AgentRowSpec(55, "memory-arbiter", "idling"),), None),
)


def test_the_arbiter_is_messaged_with_its_job(
    sdk_environment: MeivaContext, capsys: pytest.CaptureFixture[str]
) -> None:
    with serve_gateway(_ARBITER) as gateway, gateway.client() as client, use_client(client):
        wake_arbiter(_SLOT, _JOB, label="memory-arbiter")

    message = gateway.requests[-1]
    assert message.path == "/api/agents/55/messages"
    assert message.body["content"] == f"memory-arbiter: {_JOB}"
    assert f"[schedule] Agent notified: {_JOB}" in capsys.readouterr().out


def test_an_unreachable_directory_is_logged_and_does_not_stop_the_schedule(
    sdk_environment: MeivaContext, capsys: pytest.CaptureFixture[str]
) -> None:
    with serve_gateway() as gateway, gateway.client() as client, use_client(client):
        wake_arbiter(_SLOT, _JOB, label="memory-arbiter")

    assert [request.method for request in gateway.requests] == ["GET"]
    assert "[schedule] Failed:" in capsys.readouterr().out


def test_a_trigger_without_its_message_is_refused() -> None:
    with pytest.raises(ValueError, match="carries its message"):
        wake_arbiter(_SLOT, None, label="memory-arbiter")


def test_dream_instructions_use_the_vault_layout() -> None:
    source = Path(__file__).parents[1] / "memory-steward-schedule.py"
    tree = ast.parse(source.read_text())
    job = next(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("Daily Meiva Dream consolidation")
    )

    assert "git -C ~/.meiva/vault fetch origin main" in job
    assert "git -C ~/.meiva/vault worktree add" in job
    assert "ln -sfn $W $FH/vault; cd $W" in job
    assert "$W/memory/MEMORY.md" in job
    assert "files in `$W/memory`" in job
    assert "python3 $W/validate.py" in job
    assert "git -C ~/.meiva/vault worktree remove --force $W" in job
    assert "git -C ~/.meiva/vault worktree prune" in job
    assert "~/.meiva/memory" not in job
    assert "$FH/memory" not in job
