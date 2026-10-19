from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from base.lm.claude_sdk import (
    ApprovalPolicyResult,
    BridgeConfig,
    ClaudeSession,
    SessionTotals,
    TurnOutcome,
)
from base.lm.claude_sdk.process import PsutilChildProbe

WORKSPACE_POLICY = "This demo workspace is read-only for the model: the host policy denies writes."


def policy(tool_name: str, _tool_input: Mapping[str, Any]) -> ApprovalPolicyResult | None:
    if tool_name == "Write":
        return ApprovalPolicyResult(allow=False, message=WORKSPACE_POLICY)
    if tool_name == "Bash":
        return ApprovalPolicyResult(allow=True)
    return None


def make_workspace() -> Path:
    workspace = Path(tempfile.mkdtemp(prefix="bridge-demo-"))
    (workspace / "hello.txt").write_text("the quick brown fox jumps\n")
    return workspace


async def run_stage(name: str, session: ClaudeSession, prompt: str) -> tuple[TurnOutcome, float]:
    started = time.perf_counter()
    outcome = await session.submit(prompt).outcome()
    elapsed = time.perf_counter() - started
    print(f"\n[{name}] {elapsed:.2f}s  subtype={outcome.subtype}  error={outcome.is_error}")
    print(f"[{name}] model> {outcome.text[:400]}")
    return (outcome, elapsed)


async def stage_cancel(session: ClaudeSession, probe: PsutilChildProbe) -> dict[str, Any]:
    print(f"[cancel] cli_pid: {session.cli_pid}")
    ticket = session.submit(
        "Run this exact bash command with the Bash tool and wait for it to finish: ping -c 30 127.0.0.1"
    )
    started = time.perf_counter()
    child_pids = await wait_for_child(session, probe, ticket, timeout=60.0)
    observed_at = time.perf_counter()
    report = await session.interrupt()
    try:
        outcome = await asyncio.wait_for(ticket.outcome(), timeout=30.0)
    except TimeoutError:
        outcome = None
    print(f"\n[cancel] tool child seen after {observed_at - started:.2f}s: {sorted(child_pids)}")
    print(
        f"[cancel] interrupt returned in {report.interrupt_ms:.1f}ms; settled={report.settled}; waited={report.waited_ms:.0f}ms; cli_alive={report.cli_alive}; remaining={[o.pid for o in report.remaining]}"
    )
    if outcome is not None:
        print(f"[cancel] turn ended subtype={outcome.subtype} interrupted={outcome.interrupted}")
    return {
        "child_pids": sorted(child_pids),
        "interrupt_ms": report.interrupt_ms,
        "settled": report.settled,
        "waited_ms": report.waited_ms,
        "remaining": [observation.pid for observation in report.remaining],
        "cli_alive": report.cli_alive,
        "subtype": outcome.subtype if outcome is not None else "unknown",
    }


async def wait_for_child(
    session: ClaudeSession, probe: PsutilChildProbe, ticket: Any, *, timeout: float
) -> set[int]:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        pid = session.cli_pid
        children = probe.descendants(pid) if pid is not None else {}
        if children:
            return set(children)
        if ticket.settled:
            outcome = await ticket.outcome()
            raise RuntimeError(
                f"the turn ended without a tool child (subtype={outcome.subtype}): {outcome.text[:300]}"
            )
        await asyncio.sleep(0.02)
    raise TimeoutError("no tool child process appeared")


async def drain(session: ClaudeSession) -> None:
    async for _event in session.events():
        pass


def summarise(label: str, totals: SessionTotals) -> None:
    print(
        f"[{label}] turns={totals.turns} in={totals.input_tokens} out={totals.output_tokens} cache_read={totals.cache_read_input_tokens} cost=${totals.cost_usd:.4f}"
    )


async def main() -> int:
    workspace = make_workspace()
    print(f"workspace: {workspace}")
    config = BridgeConfig(cwd=workspace)
    print(f"model: {config.model}   auth: oauth (no API key forwarded)")
    started = time.perf_counter()
    session = ClaudeSession(config, environ=os.environ, approval_policy=policy)
    await session.start()
    pump = asyncio.create_task(drain(session))
    cancel: dict[str, Any] = {}
    session_id: str | None = None
    try:
        await run_stage(
            "1 talk+read",
            session,
            "Remember the number 4711. Then use the Read tool to read hello.txt in the current directory and reply with exactly the third word.",
        )
        await run_stage(
            "2 approval",
            session,
            "Use the Write tool to create a file named approval.txt with the exact content APPROVED-42, then reply with the single word DONE.",
        )
        print(f"[2 approval] approval.txt exists: {(workspace / 'approval.txt').exists()}")
        cancel = await stage_cancel(session, PsutilChildProbe())
        session_id = session.session_id
        cli_pid = session.cli_pid
        summarise("session", session.totals)
    finally:
        await session.close(reason="demo stage 1 done")
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
    cli_reaped = cli_pid is not None and (not PsutilChildProbe().is_alive(cli_pid))
    print(f"[close] cli pid {cli_pid} reaped: {cli_reaped}")
    print(f"\nsession_id: {session_id}")
    resumed = ClaudeSession(config, environ=os.environ, approval_policy=policy)
    await resumed.start(resume=session_id)
    pump = asyncio.create_task(drain(resumed))
    try:
        outcome, _ = await run_stage(
            "4 resume",
            resumed,
            "What number did I ask you to remember earlier? Reply with just the number.",
        )
        summarise("resumed", resumed.totals)
    finally:
        await resumed.close(reason="demo done")
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
    print(f"\ntotal wall clock: {time.perf_counter() - started:.2f}s")
    print(f"resume recalled 4711: {'4711' in outcome.text}")
    ok = (
        cancel["settled"] is True
        and cancel["remaining"] == []
        and cli_reaped
        and ("4711" in outcome.text)
        and (not (workspace / "approval.txt").exists())
    )
    print("RESULT:", "PASS" if ok else "CHECK ABOVE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
