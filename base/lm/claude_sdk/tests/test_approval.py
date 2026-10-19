from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from base.lm.claude_sdk import ApprovalPolicyResult
from base.lm.claude_sdk.events import ApprovalDecided, ApprovalRequested, ToolResult
from base.lm.claude_sdk.tests.fake_sdk import CallTool, Say
from base.lm.claude_sdk.tests.support import start


def allow_everything(tool_name: str, tool_input: Mapping[str, Any]) -> ApprovalPolicyResult:
    del tool_name, tool_input
    return ApprovalPolicyResult(allow=True)


async def test_policy_decides_without_a_human(tmp_path: Path) -> None:
    harness = await start(
        tmp_path,
        scripts=[[CallTool("Write", {"file_path": "a.txt"}, tool_use_id="toolu-1"), Say("ok")]],
        approval_policy=allow_everything,
    )
    try:
        outcome = await harness.session.submit("write it").outcome()
        decided = harness.of(ApprovalDecided)
        assert [event.source for event in decided] == ["policy"]
        assert decided[0].allow is True
        assert harness.of(ApprovalRequested) == []
        assert outcome.is_error is False
    finally:
        await harness.stop()


async def test_human_allow_lets_the_tool_run(tmp_path: Path) -> None:
    harness = await start(
        tmp_path,
        scripts=[
            [CallTool("Write", {"file_path": "a.txt"}, tool_use_id="toolu-1", content="wrote")]
        ],
    )
    try:
        ticket = harness.session.submit("write it")
        requested = await harness.wait_for(lambda event: isinstance(event, ApprovalRequested))
        assert requested.approval_id == "toolu-1"
        assert requested.tool_use_id == "toolu-1"
        assert harness.session.resolve_approval("toolu-1", allow=True) is True
        outcome = await ticket.outcome()
        [result] = harness.of(ToolResult)
        assert result.is_error is False
        assert result.content == "wrote"
        decided = harness.of(ApprovalDecided)[0]
        assert decided.source == "human"
        assert decided.allow is True
        assert decided.latency_ms >= 0.0
        assert outcome.is_error is False
    finally:
        await harness.stop()


async def test_unanswered_approval_denies_on_timeout(tmp_path: Path) -> None:
    harness = await start(
        tmp_path,
        scripts=[[CallTool("Bash", {"command": "rm -rf /tmp/x"}, tool_use_id="toolu-2")]],
        approval_timeout_seconds=0.05,
    )
    try:
        ticket = harness.session.submit("run it")
        await harness.wait_for(lambda event: isinstance(event, ApprovalDecided))
        decided = harness.of(ApprovalDecided)[0]
        assert decided.source == "timeout"
        assert decided.allow is False
        assert "denied by default" in (decided.message or "")
        [result] = harness.of(ToolResult)
        assert result.is_error is True
        assert "denied by default" in result.content
        assert (await ticket.outcome()).is_error is False
        assert harness.session.pending_approvals == {}
    finally:
        await harness.stop()


async def test_denial_message_reaches_the_model(tmp_path: Path) -> None:
    harness = await start(
        tmp_path, scripts=[[CallTool("Bash", {"command": "rm -rf /tmp/x"}, tool_use_id="toolu-3")]]
    )
    try:
        ticket = harness.session.submit("run it")
        await harness.wait_for(lambda event: isinstance(event, ApprovalRequested))
        harness.session.resolve_approval(
            "toolu-3", allow=False, message="Workspace policy: no deletes."
        )
        await ticket.outcome()
        [result] = harness.of(ToolResult)
        assert result.is_error is True
        assert result.content == "Workspace policy: no deletes."
        assert harness.of(ApprovalDecided)[0].source == "human"
    finally:
        await harness.stop()


async def test_unknown_approval_id_is_not_resolved(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        assert harness.session.resolve_approval("nope", allow=True) is False
    finally:
        await harness.stop()


async def test_close_denies_pending_approvals(tmp_path: Path) -> None:
    harness = await start(
        tmp_path,
        scripts=[[CallTool("Bash", {"command": "sleep 1"}, tool_use_id="toolu-4")]],
        approval_timeout_seconds=30.0,
    )
    try:
        harness.session.submit("run it")
        await harness.wait_for(lambda event: isinstance(event, ApprovalRequested))
        await harness.session.close(reason="test")
        decided = [event for event in harness.of(ApprovalDecided) if event.approval_id == "toolu-4"]
        assert decided and decided[0].allow is False
        assert decided[0].source == "closed"
    finally:
        await asyncio.sleep(0)
        await harness.stop()
