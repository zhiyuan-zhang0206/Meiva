from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from base.lm.harness import FileSessionRegistry, HarnessInput, InputRejected, SessionRecord
from base.lm.harness.session import (
    BackgroundItem,
    BackgroundKind,
    BackgroundWork,
    BackgroundWorkReporting,
    decode_items,
    describe,
    encode_items,
    lost_work_note,
)
from base.lm.harness.tests.bridge_support import run_turn
from base.lm.harness.tests.test_bridge import FakeSession, make_bridge

HARNESS = "claude_code"
NOW = time.time()


class WorkingSession(FakeSession):
    # meiva:note
    # A session that answers the two background questions: what it holds now, and what the
    # process it replaced left behind. Nothing else differs from the bridge's usual fake.
    def __init__(
        self, *, holds: tuple[BackgroundItem, ...] = (), lost: tuple[BackgroundItem, ...] = ()
    ) -> None:
        super().__init__()
        self.holds = holds
        self._lost = lost

    def background_work(self) -> BackgroundWork:
        return BackgroundWork(self.holds)

    def take_lost_background(self) -> tuple[BackgroundItem, ...]:
        lost, self._lost = self._lost, ()
        return lost


def _item(
    kind: BackgroundKind = BackgroundKind.CRON,
    text: str = "cron `* * * * *`: ping",
    *,
    until: float | None = None,
) -> BackgroundItem:
    return BackgroundItem(kind, text, until)


def test_a_description_is_one_collapsed_line_within_the_limit() -> None:
    assert describe("sleep   100;\n echo  done") == "sleep 100; echo done"
    long = describe("x" * 500)
    assert len(long) == 120 and long.endswith("…")


def test_an_item_stops_counting_after_its_until_and_none_never_ends() -> None:
    assert _item(until=None).pending_at(10**12)
    assert _item(until=NOW + 5).pending_at(NOW)
    assert not _item(until=NOW - 1).pending_at(NOW)
    work = BackgroundWork((_item(until=NOW - 1), _item(until=None)))
    assert len(work.pending_at(NOW).items) == 1


def test_the_lost_note_names_every_pending_item_and_skips_the_ended_ones() -> None:
    note = lost_work_note(
        [
            _item(BackgroundKind.COMMAND, "sleep 100"),
            _item(BackgroundKind.WAKEUP, "wakeup with the prompt: check CI", until=NOW + 60),
            _item(BackgroundKind.CRON, "already fired", until=NOW - 60),
        ],
        harness="claude_code",
        now=NOW,
    )
    assert note is not None
    assert "claude_code CLI process" in note and "restarted" in note
    assert "- background command: sleep 100" in note
    assert "- scheduled wakeup: wakeup with the prompt: check CI" in note
    assert "already fired" not in note
    assert lost_work_note([_item(until=NOW - 1)], harness="codex", now=NOW) is None
    assert lost_work_note([], harness="codex", now=NOW) is None


def test_items_survive_the_record_encoding_and_a_malformed_entry_is_refused() -> None:
    items = (_item(until=NOW + 9), _item(BackgroundKind.COMMAND, "sleep 1"))
    assert decode_items(encode_items(items), source="test") == items
    with pytest.raises(TypeError, match="must be a list"):
        _ = decode_items({"kind": "cron"}, source="test")
    with pytest.raises(TypeError, match="needs a kind"):
        _ = decode_items([{"kind": "cron"}], source="test")
    with pytest.raises(ValueError, match="not a valid BackgroundKind"):
        _ = decode_items([{"kind": "nap", "description": "x"}], source="test")
    with pytest.raises(TypeError, match="until"):
        _ = decode_items([{"kind": "cron", "description": "x", "until": "soon"}], source="test")


def test_a_record_keeps_its_background(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    base = SessionRecord(
        agent_id=6984,
        session_id="sess-1",
        cwd="/w",
        model="m",
        created_at="2026-11-11T00:00:00+00:00",
        updated_at="2026-11-11T00:00:00+00:00",
        harness=HARNESS,
    )
    registry.save(base)
    assert registry.load(6984, harness=HARNESS) == base

    with_work = replace(base, background=(_item(until=NOW + 9),))
    registry.save(with_work)
    assert registry.load(6984, harness=HARNESS) == with_work


async def test_the_first_input_after_a_restart_carries_the_lost_note_once() -> None:
    session = WorkingSession(lost=(_item(BackgroundKind.COMMAND, "sleep 100; echo BGDONE"),))
    bridge = make_bridge(session)

    _ = await run_turn(bridge, "hello", key="inbound-1")
    _ = await run_turn(bridge, "again", key="inbound-2")

    first, second = (text for text, _, _ in session.submitted)
    assert first.startswith("System note: the claude_code CLI process")
    assert "sleep 100; echo BGDONE" in first
    assert first.endswith("\n\nhello")
    assert second == "again"


async def test_an_ended_timer_is_not_reported_as_lost() -> None:
    session = WorkingSession(lost=(_item(until=time.time() - 60),))
    bridge = make_bridge(session)

    _ = await run_turn(bridge, "hello", key="inbound-1")

    assert session.submitted[0][0] == "hello"


async def test_a_session_that_cannot_tell_has_nothing_pending_and_nothing_lost() -> None:
    session = FakeSession()
    bridge = make_bridge(session)
    assert not isinstance(session, BackgroundWorkReporting)
    assert not bridge.background_work()

    _ = await run_turn(bridge, "hello", key="inbound-1")

    assert session.submitted[0][0] == "hello"
    assert not bridge.background_work()


async def test_the_bridge_reports_what_its_live_session_holds() -> None:
    held = (_item(BackgroundKind.AGENT, "sub-agent reviewing the diff"),)
    session = WorkingSession(holds=held)
    bridge = make_bridge(session)
    assert not bridge.background_work(), "no session yet: nothing is held"

    _ = await run_turn(bridge, "hello", key="inbound-1")

    assert bridge.background_work().items == held
    session.holds = ()
    assert not bridge.background_work()


class RefusingFirstWorkingSession(WorkingSession):
    def __init__(self, *, lost: tuple[BackgroundItem, ...] = ()) -> None:
        super().__init__(lost=lost)
        self.refused = False

    async def deliver(self, inputs: Sequence[HarnessInput]) -> None:
        if not self.refused:
            self.refused = True
            self.submitted.append((inputs[0].text, inputs[0].key, "refused"))
            self._queue.append(InputRejected(inputs[0].key, "no active turn", 1))
            return
        await super().deliver(inputs)


async def test_a_refused_input_hands_the_lost_note_over_again_until_it_is_in_context() -> None:
    session = RefusingFirstWorkingSession(lost=(_item(BackgroundKind.COMMAND, "sleep 100"),))
    bridge = make_bridge(session)
    subscription = await bridge.subscribe()
    try:
        await bridge.deliver([HarnessInput(key="inbound-1", text="one")])
        await asyncio.sleep(0.05)
        await bridge.deliver([HarnessInput(key="inbound-1", text="one")])
        await asyncio.sleep(0.05)
        await bridge.deliver([HarnessInput(key="inbound-2", text="two")])
    finally:
        subscription.close()

    texts = [text for text, _key, _turn in session.submitted]
    assert "sleep 100" in texts[0], "the refused delivery carried the note"
    assert "sleep 100" in texts[1], "so does the delivery that follows the refusal"
    assert texts[2] == "two", "once the input is in the context it is not repeated"
