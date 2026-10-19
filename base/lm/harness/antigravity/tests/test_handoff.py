from __future__ import annotations

from pathlib import Path

from base.lm.claude_sdk.events import HandoffInjected
from base.lm.claude_sdk.handoff import Handoff
from base.lm.claude_sdk.registry import FileSessionRegistry, SessionRecord
from base.lm.harness.antigravity.handoff_rules import SHELL_SEMANTICS, build_handoff
from base.lm.harness.antigravity.tests.support import say, start


def a_handoff(workspace: Path | None = None) -> Handoff:
    return build_handoff(
        agent_id=7,
        instruction="do the thing",
        machine="ubuntu",
        workspace=workspace or Path("ws"),
    )


def test_the_adapter_handoff_carries_the_shell_semantics() -> None:
    rendered = a_handoff().render()
    for rule in SHELL_SEMANTICS:
        assert rule in rendered
    assert "fresh shell" in rendered
    assert "setsid" in rendered


def test_a_plain_handoff_does_not_carry_them() -> None:
    plain = Handoff(agent_id=7, instruction="x", machine="ubuntu", workspace=Path("ws"))
    assert "setsid" not in plain.render()


async def test_the_handoff_is_the_first_message_exactly_once(tmp_path: Path) -> None:
    harness = await start(tmp_path, turns=[say("ack"), say("one")], agent_id=7, handoff=a_handoff())
    try:
        ticket = harness.session.handoff_ticket
        assert ticket is not None
        await ticket.outcome()
        assert len(harness.of(HandoffInjected)) == 1
        await harness.wait_until(lambda: len(harness.messages()) == 1)
        assert "agent id: 7" in harness.messages()[0]
    finally:
        await harness.stop()


async def test_an_explicit_resume_is_not_treated_as_a_fresh_session(tmp_path: Path) -> None:
    harness = await start(
        tmp_path, turns=[say("one")], resume="conv-fake", handoff=a_handoff()
    )
    try:
        assert harness.session.resumed is True
        assert harness.session.handoff_ticket is None
        assert harness.of(HandoffInjected) == []
        await harness.wait_until(lambda: harness.argv_seen() != [])
        assert "conv-fake" in harness.argv_seen()
    finally:
        await harness.stop()


async def test_a_resumed_session_does_not_reinject(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "reg")
    registry.save(
        SessionRecord(
            agent_id=7,
            session_id="conv-fake",
            cwd=str(tmp_path),
            model="gemini-4-argon-high",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
    )
    harness = await start(
        tmp_path, turns=[say("one")], agent_id=7, registry=registry, handoff=a_handoff()
    )
    try:
        assert harness.session.handoff_ticket is None
        assert harness.of(HandoffInjected) == []
        await harness.wait_until(lambda: harness.argv_seen() != [])
        assert "--conversation" in harness.argv_seen()
        assert "conv-fake" in harness.argv_seen()
    finally:
        await harness.stop()
