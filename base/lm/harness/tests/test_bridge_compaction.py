from __future__ import annotations

from pathlib import Path

from base.lm.harness import ContextCompacted
from base.lm.harness.handoff import compaction_reminder
from base.lm.harness.tests.test_bridge import FakeSession, handoff, make_bridge


async def test_a_compaction_adds_a_reminder_to_the_next_turns_opening_input_only() -> None:
    session = FakeSession(during=[ContextCompacted(turn_id="t1")])
    doc = handoff().with_reference_file(Path("/agents/6984/meiva_instructions.md"))
    bridge = make_bridge(session, handoff=doc)

    _ = await bridge.run_turn("one", key="inbound-1")
    session._during = ()
    _ = await bridge.run_turn("two", key="inbound-2")
    _ = await bridge.run_turn("three", key="inbound-3")

    reminder = compaction_reminder(Path("/agents/6984/meiva_instructions.md"))
    assert [text for text, _, _ in session.submitted] == [
        f"{doc.render().strip()}\n\none",
        f"{reminder}\n\ntwo",
        "three",
    ]
    assert "/agents/6984/meiva_instructions.md" in reminder


async def test_a_compaction_without_a_saved_instructions_file_adds_no_reminder() -> None:
    session = FakeSession(during=[ContextCompacted()])
    bridge = make_bridge(session, handoff=handoff())

    _ = await bridge.run_turn("one", key="inbound-1")
    _ = await bridge.run_turn("two", key="inbound-2")

    assert session.submitted[1][0] == "two"


async def test_a_new_handoff_replaces_the_file_the_reminder_names() -> None:
    session = FakeSession(during=[ContextCompacted()])
    first = handoff().with_reference_file(Path("/agents/6984/old.md"))
    bridge = make_bridge(session, handoff=first)

    _ = await bridge.run_turn("one", key="inbound-1")
    bridge.set_handoff(handoff().with_reference_file(Path("/agents/6984/new.md")))
    _ = await bridge.run_turn("two", key="inbound-2")

    assert "/agents/6984/new.md" in session.submitted[1][0]
    assert "old.md" not in session.submitted[1][0]
