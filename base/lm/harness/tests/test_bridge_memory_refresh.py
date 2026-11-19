from __future__ import annotations

import asyncio
import os
import random
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from base.api_contracts.agent_contract import Harness
from base.clock.tests.fakes import system_clock
from base.lm.harness import (
    ContextRefresh,
    HarnessBridge,
    HarnessInput,
    InputRejected,
    RefreshContext,
)
from base.lm.harness.handoff import Handoff
from base.lm.harness.registration import SecureRandom
from base.lm.harness.tests.bridge_support import run_turn
from base.lm.harness.tests.test_bridge import FakeSession
from base.lm.harness.wiring import HANDOFF_INSTRUCTION

AGENT = 6984


def _index(home: Path) -> Path:
    return home / "vault" / "memory" / "MEMORY.md"


def _refreshes(ctx: RefreshContext) -> tuple[ContextRefresh, ...]:
    path = _index(ctx.home)
    text = path.read_text(encoding="utf-8")
    return (ContextRefresh(key="memory", body=f"BODY {text}", notice=f"NOTICE re-read {path}"),)


class KeepingSession(FakeSession):
    # meiva:note
    # A fake that keeps `context_digests` the way an adapter keeps them on its record: the dict
    # passed in stands for the record, so a second session built on it is a resume of the same
    # transcript and one built on a fresh dict is a fork or a new session.
    def __init__(self, record: dict[str, str], *, resumed: bool) -> None:
        super().__init__(resumed=resumed)
        self.record = record

    @property
    def context_digests(self) -> Mapping[str, str]:
        return dict(self.record)

    def record_context_digests(self, digests: Mapping[str, str]) -> None:
        self.record.update(digests)


def _write(home: Path, text: str) -> None:
    _index(home).parent.mkdir(parents=True, exist_ok=True)
    _ = _index(home).write_text(text, encoding="utf-8")


def _bridge(
    home: Path, session: FakeSession, *, doc: Handoff | None = None, stamp: str = ""
) -> HarnessBridge:
    return HarnessBridge(
        Harness.CLAUDE_CODE,
        AGENT,
        lambda _handle: session,
        machine="macmini",
        handoff=doc,
        home=home,
        workspace=home / "workspace",
        refreshes=(_refreshes,),
        registration_ttl_seconds=3600.0,
        clock=system_clock(),
        handle_rng=SecureRandom(random.SystemRandom()),
        controller_pid=os.getpid(),
        timestamp_prefix=lambda: stamp,
    )


def _handoff(home: Path) -> Handoff:
    return Handoff(
        agent_id=AGENT,
        instruction=HANDOFF_INSTRUCTION,
        machine="macmini",
        workspace=home / "workspace",
        home=home,
    )


def _texts(session: FakeSession) -> list[str]:
    return [text for text, _, _ in session.submitted]


async def test_a_fresh_session_counts_the_handoff_as_given_and_then_gets_one_line_per_edit(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "v1")
    doc = _handoff(tmp_path)
    record: dict[str, str] = {}
    session = KeepingSession(record, resumed=False)
    bridge = _bridge(tmp_path, session, doc=doc)

    await run_turn(bridge, "one", key="k1")
    await run_turn(bridge, "two", key="k2")
    _write(tmp_path, "v2")
    await run_turn(bridge, "three", key="k3")
    await run_turn(bridge, "four", key="k4")

    one, two, three, four = _texts(session)
    assert one == f"{doc.render().strip()}\n\none"
    assert two == "two"
    assert three == f"NOTICE re-read {_index(tmp_path)}\n\nthree"
    assert "BODY" not in three
    assert four == "four"
    assert set(record) == {"memory"}


# meiva:note
# A resumed session from before the record kept digests, and a fork (whose new record starts
# empty), both reach the bridge as a session with no digest for the key.
@pytest.mark.parametrize("stamp", ["", "[2026-11-19 Thu 19:20:00] "])
async def test_a_session_that_never_saw_the_body_gets_it_once_and_a_resume_keeps_that(
    tmp_path: Path, stamp: str
) -> None:
    _write(tmp_path, "v1")
    record: dict[str, str] = {}
    first = KeepingSession(record, resumed=True)
    bridge = _bridge(tmp_path, first, doc=_handoff(tmp_path), stamp=stamp)

    await run_turn(bridge, "one", key="k1")
    await run_turn(bridge, "two", key="k2")
    _write(tmp_path, "v2")
    await run_turn(bridge, "three", key="k3")

    one, two, three = _texts(first)
    assert one == f"{stamp}BODY v1\n\none"
    assert "# Role & Operating Context" not in one
    assert two == "two"
    assert three == f"{stamp}NOTICE re-read {_index(tmp_path)}\n\nthree"

    second = KeepingSession(record, resumed=True)
    await run_turn(_bridge(tmp_path, second, stamp=stamp), "four", key="k4")
    assert _texts(second) == ["four"]


async def test_a_refused_opening_input_records_nothing_and_the_next_one_carries_it_again(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "v1")

    class RefusesOnce(KeepingSession):
        refused = False

        async def deliver(self, inputs: Sequence[HarnessInput]) -> None:
            if not self.refused:
                self.refused = True
                self.submitted.append((inputs[0].text, inputs[0].key, "refused"))
                self._queue.append(InputRejected(inputs[0].key, "busy", 1))
                return
            await super().deliver(inputs)

    record: dict[str, str] = {}
    session = RefusesOnce(record, resumed=True)
    bridge = _bridge(tmp_path, session)
    subscription = await bridge.subscribe()
    try:
        await bridge.deliver([HarnessInput.of("k1", "hello")])
        await asyncio.sleep(0.05)
        assert record == {}
    finally:
        subscription.close()
    await run_turn(bridge, "hello", key="k1")
    await run_turn(bridge, "next", key="k2")

    assert _texts(session) == ["BODY v1\n\nhello", "BODY v1\n\nhello", "next"]
    assert set(record) == {"memory"}


async def test_a_session_that_keeps_no_digests_is_given_no_refreshed_context(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "v1")
    session = FakeSession(resumed=True)
    bridge = _bridge(tmp_path, session)

    await run_turn(bridge, "one", key="k1")
    _write(tmp_path, "v2")
    await run_turn(bridge, "two", key="k2")

    assert _texts(session) == ["one", "two"]
