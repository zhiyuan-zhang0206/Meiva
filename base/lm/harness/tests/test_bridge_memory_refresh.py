from __future__ import annotations

import asyncio
import hashlib
import os
import random
from collections.abc import Sequence
from pathlib import Path

import pytest

from base.api_contracts.agent_contract import Harness
from base.clock.tests.fakes import system_clock
from base.lm.harness import (
    ContextRefresh,
    FileSessionDigestStore,
    HarnessBridge,
    HarnessInput,
    InputRejected,
    RefreshContext,
    SessionStarted,
)
from base.lm.harness.handoff import Handoff, instructions_path
from base.lm.harness.registration import SecureRandom
from base.lm.harness.tests.bridge_support import run_turn
from base.lm.harness.tests.test_bridge import FakeSession, handoff
from base.lm.harness.wiring import HANDOFF_INSTRUCTION


def _memory_refresh(ctx: RefreshContext) -> ContextRefresh:
    shared_path = ctx.home / "vault" / "memory" / "MEMORY.md"
    shared = shared_path.read_text(encoding="utf-8").strip() if shared_path.is_file() else ""
    personal_path = ctx.workspace / "memory" / "MEMORY.md"
    personal = personal_path.read_text(encoding="utf-8").strip() if personal_path.is_file() else ""
    digest = hashlib.sha256(f"{shared}\0{personal}".encode()).hexdigest()
    where = f" (`{ctx.instructions_file}` updated)" if ctx.instructions_file else ""
    return ContextRefresh(
        key="meiva_memory",
        digest=digest,
        text=f"[Meiva notice] Memory Index refreshed{where}.\n\n{shared}\n{personal}".strip(),
    )


def _bridge(
    tmp_path: Path,
    session: FakeSession,
    *,
    doc: Handoff | None = None,
    stamp: str = "",
    store: FileSessionDigestStore | None = None,
) -> tuple[HarnessBridge, FileSessionDigestStore]:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    digest_store = store or FileSessionDigestStore(tmp_path / "sessions")
    bridge = HarnessBridge(
        Harness.CLAUDE_CODE,
        6984,
        lambda _handle: session,
        machine="macmini",
        handoff=doc,
        home=home,
        workspace=workspace,
        refreshes=(_memory_refresh,),
        digest_store=digest_store,
        registration_ttl_seconds=3600.0,
        clock=system_clock(),
        handle_rng=SecureRandom(random.SystemRandom()),
        controller_pid=os.getpid(),
        timestamp_prefix=lambda: stamp,
    )
    return bridge, digest_store


def _write_shared(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "home" / "vault" / "memory" / "MEMORY.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


async def test_fresh_session_records_digest_without_duplicate_refresh(tmp_path: Path) -> None:
    _write_shared(tmp_path, "## \u8bed\u8a00\uff1a\u4e2d\u6587")
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    doc = Handoff(
        agent_id=6984,
        instruction=HANDOFF_INSTRUCTION,
        machine="macmini",
        workspace=workspace,
        home=tmp_path / "home",
    ).with_reference_file(instructions_path(workspace))
    session = FakeSession()
    bridge, store = _bridge(tmp_path, session, doc=doc)

    await run_turn(bridge, "first", key="k1")
    await run_turn(bridge, "second", key="k2")

    assert session.submitted[0][0] == f"{doc.render().strip()}\n\nfirst"
    assert "[Meiva notice] Memory Index refreshed" not in session.submitted[0][0]
    assert session.submitted[1][0] == "second"
    assert store.load(6984, "claude_code", session_id="session-1").get("meiva_memory")


@pytest.mark.parametrize("stamp", ["", "[2026-11-19 Thu 19:20:00] "])
async def test_resumed_session_state_machine_refreshes_only_when_digest_missing_or_changed(
    tmp_path: Path, stamp: str
) -> None:
    shared_file = _write_shared(tmp_path, "v1-shared")
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    inst = instructions_path(workspace)
    doc = handoff().with_reference_file(inst)

    class _LateInitResumedSession(FakeSession):
        async def start(self, *, resume: str | None = None) -> None:
            self.started += 1
            self.session_id = "legacy-sid"

    session1 = _LateInitResumedSession(resumed=True)
    store = FileSessionDigestStore(tmp_path / "sessions")
    bridge1, _ = _bridge(tmp_path, session1, doc=doc, stamp=stamp, store=store)

    await run_turn(bridge1, "one", key="k1")
    await run_turn(bridge1, "two", key="k2")
    shared_file.write_text("v2-shared", encoding="utf-8")
    await run_turn(bridge1, "three", key="k3")
    await run_turn(bridge1, "four", key="k4")

    session2 = _LateInitResumedSession(resumed=True)
    bridge2, _ = _bridge(tmp_path, session2, doc=doc, stamp=stamp, store=store)
    await run_turn(bridge2, "five", key="k5")

    t1, t2, t3, t4 = [text for text, _, _ in session1.submitted]
    assert "# Role & Operating Context" not in t1
    assert t1.startswith(f"{stamp}[Meiva notice] Memory Index refreshed (`{inst}` updated)")
    assert "v1-shared" in t1 and t1.endswith("\n\none")
    assert t2 == "two"
    assert t3.startswith(f"{stamp}[Meiva notice] Memory Index refreshed (`{inst}` updated)")
    assert "v2-shared" in t3 and t3.endswith("\n\nthree")
    assert t4 == "four"
    assert session2.submitted[0][0] == "five"


async def test_rejected_refresh_does_not_commit_digest_until_in_context(tmp_path: Path) -> None:
    _write_shared(tmp_path, "v1-shared")

    class _RejectOnceResumedSession(FakeSession):
        def __init__(self) -> None:
            super().__init__(resumed=True)
            self.rejected = False

        async def start(self, *, resume: str | None = None) -> None:
            self.started += 1

        async def deliver(self, inputs: Sequence[HarnessInput]) -> None:
            if not self.rejected:
                self.rejected = True
                self.submitted.append((inputs[0].text, inputs[0].key, "refused"))
                self._queue.append(InputRejected(inputs[0].key, "busy", 1))
                return
            self.session_id = "sid-after-init"
            self._queue.append(
                SessionStarted(session_id="sid-after-init", model="claude-sonnet-5-5", cwd="/w")
            )
            await super().deliver(inputs)

    session = _RejectOnceResumedSession()
    bridge, store = _bridge(tmp_path, session)
    sub = await bridge.subscribe()
    try:
        await bridge.deliver([HarnessInput.of("k1", "hello")])
        await asyncio.sleep(0.05)
        assert store.load(6984, "claude_code", session_id="sid-after-init") == {}
        await bridge.deliver([HarnessInput.of("k1", "hello")])
        await asyncio.sleep(0.05)
        await bridge.deliver([HarnessInput.of("k2", "next")])
        await asyncio.sleep(0.05)
    finally:
        sub.close()

    assert "[Meiva notice] Memory Index refreshed" in session.submitted[0][0]
    assert "[Meiva notice] Memory Index refreshed" in session.submitted[1][0]
    assert session.submitted[2][0] == "next"
    assert store.load(6984, "claude_code", session_id="sid-after-init").get("meiva_memory")


async def test_forked_session_with_new_session_id_refreshes_and_initializes_its_own_digest(
    tmp_path: Path,
) -> None:
    _write_shared(tmp_path, "shared-for-fork")
    store = FileSessionDigestStore(tmp_path / "sessions")
    store.save(6984, "claude_code", session_id="parent-sid", digests={"meiva_memory": "old"})

    class _ForkedSession(FakeSession):
        async def start(self, *, resume: str | None = None) -> None:
            self.started += 1
            self.session_id = "fork-child-sid"
            self._queue.append(
                SessionStarted(session_id="fork-child-sid", model="claude-sonnet-5-5", cwd="/w")
            )

    session = _ForkedSession(resumed=True)
    doc = handoff().with_reference_file(instructions_path(tmp_path / "workspace"))
    bridge, _ = _bridge(tmp_path, session, doc=doc, store=store)

    await run_turn(bridge, "fork marker + child task", key="k-fork")
    await run_turn(bridge, "follow-up", key="k-next")

    assert "# Role & Operating Context" not in session.submitted[0][0]
    assert "[Meiva notice] Memory Index refreshed" in session.submitted[0][0]
    assert "shared-for-fork" in session.submitted[0][0]
    assert session.submitted[1][0] == "follow-up"
    assert store.load(6984, "claude_code", session_id="fork-child-sid").get("meiva_memory")
