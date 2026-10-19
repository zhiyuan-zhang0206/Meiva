#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import platform
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from base.lm.harness import ClusterFact, EntryPoint, FileSessionRegistry, Handoff
from base.lm.harness.claude import (
    BridgeConfig,
    ClaudeSession,
    SessionTotals,
    TurnOutcome,
)
from base.lm.harness.process import PsutilChildProbe

# meiva:note
# One command, one real session, the four things the bridge is for: the handoff
# document is injected as the first message, agent id ↔ session id lands in the
# registry, a running tool is cancelled with the child reaping asserted, and a new
# process resumes the same session from the registry (no handoff re-injection).
# meiva:link demos/claude-sdk-bridge/README.md
DEMO_AGENT_ID = 6984


def build_handoff(workspace: Path) -> Handoff:
    return Handoff(
        agent_id=DEMO_AGENT_ID,
        instruction="Reply with the single word READY. The host sends your task next.",
        machine=platform.node() or "unknown",
        workspace=workspace,
        label="claude-sdk-bridge-demo",
        cluster=(ClusterFact("macmini", "hosts the Claude subscription login"),),
        entries=(
            EntryPoint("meiva.files", "read and write files under the workspace"),
            EntryPoint("meiva.shell", "run commands and open sessions"),
            EntryPoint("meiva.tasks", "the shared task registry"),
        ),
    )


def make_workspace() -> Path:
    workspace = Path(tempfile.mkdtemp(prefix="bridge-demo-"))
    (workspace / "hello.txt").write_text("the quick brown fox jumps\n")
    return workspace


async def run_stage(name: str, session: ClaudeSession, prompt: str) -> tuple[TurnOutcome, float]:
    started = time.perf_counter()
    outcome = await session.submit(prompt).outcome()
    elapsed = time.perf_counter() - started
    print(f"\n[{name}] {elapsed:.2f}s  subtype={outcome.subtype}  error={outcome.is_error}")
    print(f"[{name}] model> {outcome.text[:300]}")
    return outcome, elapsed


async def stage_cancel(session: ClaudeSession, probe: PsutilChildProbe) -> dict[str, Any]:
    ticket = session.submit(
        "Run this exact bash command with the Bash tool and wait for it to finish: "
        "ping -c 30 127.0.0.1"
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
        f"[cancel] interrupt returned in {report.interrupt_ms:.1f}ms; settled={report.settled}; "
        f"waited={report.waited_ms:.0f}ms; cli_alive={report.cli_alive}; "
        f"remaining={[observation.pid for observation in report.remaining]}"
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
                f"the turn ended without a tool child (subtype={outcome.subtype}): "
                f"{outcome.text[:300]}"
            )
        await asyncio.sleep(0.02)
    raise TimeoutError("no tool child process appeared")


async def drain(session: ClaudeSession) -> None:
    async for _event in session.events():
        pass


def summarise(label: str, totals: SessionTotals) -> None:
    print(
        f"[{label}] turns={totals.turns} in={totals.input_tokens} out={totals.output_tokens} "
        f"cache_read={totals.cache_read_input_tokens} cost=${totals.cost_usd:.4f}"
    )


async def main() -> int:
    workspace = make_workspace()
    registry = FileSessionRegistry(workspace / "sessions")
    handoff = build_handoff(workspace)
    print(f"workspace: {workspace}")
    print(f"model: {BridgeConfig(cwd=workspace).model}   auth: oauth (no API key forwarded)")
    print(f"\n[handoff] {handoff.size_bytes()} bytes injected as the first message:")
    print("\n".join(handoff.render().splitlines()[:8]) + "\n  …")

    started = time.perf_counter()
    session = ClaudeSession(
        BridgeConfig(cwd=workspace), environ=os.environ, agent_id=DEMO_AGENT_ID, registry=registry
    )
    await session.start(handoff=handoff)
    pump = asyncio.create_task(drain(session))
    cancel: dict[str, Any] = {}
    session_id: str | None = None
    try:
        ticket = session.handoff_ticket
        if ticket is not None:
            ack = await ticket.outcome()
            print(f"\n[1 handoff] {ack.text[:200]}")
        await run_stage(
            "1 talk+read",
            session,
            "Remember the number 4711. Then use the Read tool to read hello.txt in the "
            "current directory and reply with exactly the third word.",
        )
        record = registry.load(DEMO_AGENT_ID)
        print(f"\n[2 mapping] {registry.path(DEMO_AGENT_ID)}")
        print(
            f"[2 mapping] agent {DEMO_AGENT_ID} -> session {record.session_id if record else None} "
            f"(model={record.model if record else None}, resumes={record.resumes if record else None})"
        )
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

    resumed = ClaudeSession(
        BridgeConfig(cwd=workspace), environ=os.environ, agent_id=DEMO_AGENT_ID, registry=registry
    )
    await resumed.start(handoff=handoff)
    pump = asyncio.create_task(drain(resumed))
    try:
        outcome, _ = await run_stage(
            "3 resume",
            resumed,
            "What number did I ask you to remember earlier? Reply with just the number.",
        )
        print(
            f"[3 resume] resumed={resumed.resumed} session_id={resumed.session_id} "
            f"handoff_reinjected={resumed.handoff_ticket is not None}"
        )
        summarise("resumed", resumed.totals)
    finally:
        await resumed.close(reason="demo done")
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)

    print(f"\ntotal wall clock: {time.perf_counter() - started:.2f}s")
    recalled = "4711" in outcome.text
    print(f"resume recalled 4711: {recalled}")
    ok = (
        cancel["settled"] is True
        and cancel["remaining"] == []
        and cli_reaped
        and recalled
        and resumed.resumed
        and resumed.session_id == session_id
    )
    print("RESULT:", "PASS" if ok else "CHECK ABOVE")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
