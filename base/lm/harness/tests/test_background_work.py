from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from base.api_contracts.agent_contract import Harness
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.clock.tests.manual_timers import ManualTimers
from base.clock.tests.seeded_rng import seeded_rng
from base.lm.harness import (
    FileSessionRegistry,
    HarnessBridge,
    HarnessInput,
    InputRejected,
    SessionRecord,
    ToolResult,
    UnsolicitedOutput,
)
from base.lm.harness.antigravity.tests.support import make_session
from base.lm.harness.registration import SecureRandom
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
from base.lm.harness.tests.test_bridge import FakeSession, handoff, make_bridge

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
        generation=0,
    )
    registry.save(base)
    assert registry.load(6984, harness=HARNESS) == base

    with_work = replace(base, background=(_item(until=NOW + 9),))
    registry.save(with_work)
    assert registry.load(6984, harness=HARNESS) == with_work


@pytest.mark.parametrize("stamp", ["", "[2026-11-14 Sat 16:05:51] "])
async def test_the_first_input_after_a_restart_carries_the_lost_note_once(stamp: str) -> None:
    session = WorkingSession(lost=(_item(BackgroundKind.COMMAND, "sleep 100; echo BGDONE"),))
    bridge = make_bridge(session, stamp=stamp)

    _ = await run_turn(bridge, "hello", key="inbound-1")
    _ = await run_turn(bridge, "again", key="inbound-2")

    first, second = (text for text, _, _ in session.submitted)
    assert first.startswith(f"{stamp}System note: the claude_code CLI process")
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
        await bridge.deliver([HarnessInput.of(key="inbound-1", text="one")])
        await asyncio.sleep(0.05)
        await bridge.deliver([HarnessInput.of(key="inbound-1", text="one")])
        await asyncio.sleep(0.05)
        await bridge.deliver([HarnessInput.of(key="inbound-2", text="two")])
    finally:
        subscription.close()

    texts = [text for text, _key, _turn in session.submitted]
    assert "sleep 100" in texts[0], "the refused delivery carried the note"
    assert "sleep 100" in texts[1], "so does the delivery that follows the refusal"
    assert texts[2] == "two", "once the input is in the context it is not repeated"


# meiva:note
# The opening input is read top to bottom as: the lost-work note, then the handoff (or, after a
# compaction, the reminder), then the input's own text, which after a restart can be the graph's
# resume note for a reconciled input.
# meiva:link base/lm/harness/docs/background-work.md
async def test_the_lost_note_comes_before_the_handoff_and_the_input_text() -> None:
    session = WorkingSession(lost=(_item(BackgroundKind.COMMAND, "sleep 100"),))
    doc = handoff()
    bridge = make_bridge(session, handoff=doc)

    _ = await run_turn(bridge, "resume note", key="reconciled-resume-1")

    text = session.submitted[0][0]
    rendered = doc.render().strip()
    assert text.startswith("System note: the claude_code CLI process")
    assert text.index("sleep 100") < text.index(rendered) < text.index("resume note")
    assert text.endswith("\n\nresume note")


async def _complete_short_timer(bridge: HarnessBridge, root: Path) -> None:
    subscription = await bridge.subscribe()
    try:
        (root / "release-tool").touch()
        async with asyncio.timeout(5):
            while (event := await subscription.get()) is not None:
                if isinstance(event, ToolResult) and event.tool_use_id == "timer":
                    return
        raise AssertionError("the short timer's tool completion was not delivered")
    finally:
        subscription.close()


def _prompt_texts(root: Path) -> list[str]:
    frames = [json.loads(line) for line in (root / "frames.jsonl").read_text().splitlines()]
    return [
        frame["params"]["prompt"][0]["text"]
        for frame in frames
        if frame.get("method") == "session/prompt"
    ]


async def _send_during_timer_expiry(bridge: HarnessBridge, root: Path, wall: ManualTimers) -> None:
    async with asyncio.timeout(8), asyncio.TaskGroup() as tasks:
        queued = tasks.create_task(run_turn(bridge, "continue", key="first-after-expiry"))
        await asyncio.sleep(0)
        assert not queued.done() and len(_prompt_texts(root)) == 1
        wall.advance(31)
        while not (root / "load-waiting").exists():
            await asyncio.sleep(0.005)
        assert len(_prompt_texts(root)) == 1
        wall.advance(610)
        (root / "release-load").touch()
    assert queued.result().text == "OK"


async def test_first_waiting_agy_prompt_reports_other_work_lost_during_timer_recycle(
    tmp_path: Path,
) -> None:
    wall = ManualTimers()
    clock = Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS, now=wall.now)
    wakes: list[bool] = []
    unsolicited: list[UnsolicitedOutput] = []
    session = make_session(
        tmp_path,
        clock=clock,
        extra_args=("--test-two-timers", "--test-gate-load"),
        unsolicited=unsolicited.append,
    )
    bridge = HarnessBridge(
        Harness.ANTIGRAVITY,
        1,
        lambda _: session,
        timestamp_prefix=lambda: "",
        registration_ttl_seconds=3600,
        clock=clock,
        handle_rng=SecureRandom(seeded_rng(1)),
        controller_pid=1,
        own_turn=lambda: wakes.append(True),
    )
    try:
        _ = await run_turn(bridge, "background", key="start-timers")
        assert len(bridge.background_work().items) == 2
        old_pid = session.cli_pid
        await _complete_short_timer(bridge, tmp_path)
        await _send_during_timer_expiry(bridge, tmp_path, wall)
        assert session.cli_pid is not None and session.cli_pid != old_pid
        assert session.generation == 2
        _ = await run_turn(bridge, "next", key="second-after-expiry")
        _, first, second = _prompt_texts(tmp_path)
        assert "background work below was still pending and is lost" in first
        assert first.count("keep checking the long-running build") == 1
        assert "keep checking the long-running build" not in second
        assert wakes == [] and unsolicited == []
    finally:
        await bridge.close()
