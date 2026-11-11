from __future__ import annotations

from pathlib import Path

from base.lm.harness.claude.mapping import map_message
from base.lm.harness.claude.tests import fake_sdk as fake
from base.lm.harness.claude.tests.support import replay, start
from base.lm.harness.delivery.tests.sent import send
from base.lm.harness.events import (
    AssistantText,
    HarnessCause,
    ReasoningText,
    SubagentText,
    ToolResult,
    ToolUse,
    TurnCompleted,
    TurnStarted,
)

AGENT_CALL = "toolu_AGENT"


def test_a_subagent_text_block_is_subagent_text_and_its_thinking_is_dropped() -> None:
    message = fake.AssistantMessage(
        content=[fake.ThinkingBlock(thinking="plan"), fake.TextBlock(text="ALPHA")],
        parent_tool_use_id=AGENT_CALL,
    )
    assert map_message(message, turn_id="turn-1") == [
        SubagentText(turn_id="turn-1", tool_use_id=AGENT_CALL, agent=None, text="ALPHA")
    ]


def test_a_subagents_tool_call_and_result_name_the_call_that_started_it() -> None:
    call = fake.AssistantMessage(
        content=[fake.ToolUseBlock(id="toolu_BASH", name="Bash", input={"command": "echo hi"})],
        parent_tool_use_id=AGENT_CALL,
    )
    result = fake.UserMessage(
        content=[fake.ToolResultBlock(tool_use_id="toolu_BASH", content="hi")],
        parent_tool_use_id=AGENT_CALL,
    )
    assert map_message(call, turn_id="turn-1") == [
        ToolUse("turn-1", "toolu_BASH", "Bash", {"command": "echo hi"}, AGENT_CALL)
    ]
    assert map_message(result, turn_id="turn-1") == [
        ToolResult("turn-1", "toolu_BASH", False, "hi", AGENT_CALL)
    ]


def test_the_main_agents_own_tool_call_names_no_subagent() -> None:
    call = fake.AssistantMessage(content=[fake.ToolUseBlock(id="toolu_X", name="Bash", input={})])
    [event] = map_message(call, turn_id="turn-1")
    assert isinstance(event, ToolUse) and event.parent_tool_use_id is None


def test_the_main_agents_own_text_and_thinking_are_unchanged() -> None:
    message = fake.AssistantMessage(
        content=[fake.ThinkingBlock(thinking="plan"), fake.TextBlock(text="ALPHA")]
    )
    assert map_message(message, turn_id="turn-1") == [
        ReasoningText(turn_id="turn-1", text="plan"),
        AssistantText(turn_id="turn-1", text="ALPHA"),
    ]


def test_a_subagent_token_stream_is_not_the_main_agents_text() -> None:
    delta = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "AL"}}
    message = fake.StreamEvent(event=delta, parent_tool_use_id=AGENT_CALL)
    assert map_message(message, turn_id="turn-1") == []


async def test_a_background_subagent_speaks_after_its_launching_turn_ended(tmp_path: Path) -> None:
    harness = await start(tmp_path, passive=True)
    try:
        ticket = await send(harness, "spawn", key="first")
        replay(harness.client, "subagent_background", "launch", first="first")
        launched = await ticket.outcome()
        assert launched.text == "The background agent is running."
        replay(harness.client, "subagent_background", "subagent")
        answer = await harness.wait_for(
            lambda e: isinstance(e, TurnCompleted) and e.turn_id != ticket.turn_id
        )
        texts = harness.of(SubagentText)
        assert [(e.tool_use_id, e.agent) for e in texts] == [(AGENT_CALL, "general-purpose")] * 2
        assert [e.text.split("\n")[0] for e in texts] == [
            "I will now execute this sequence.",
            "BETA",
        ]
        [_, woken] = harness.of(TurnStarted)
        assert woken.cause is HarnessCause.BACKGROUND_TASK
        assert {e.turn_id for e in texts} == {woken.turn_id} == {answer.turn_id}
        assert answer.text == "DONE", "the sub-agent's words are not the turn's reply"
        assert [e.text for e in harness.of(AssistantText) if e.turn_id == woken.turn_id] == ["DONE"]
        assert [
            (e.name, e.parent_tool_use_id)
            for e in harness.of(ToolUse)
            if e.turn_id == woken.turn_id
        ] == [("Bash", AGENT_CALL)]
        assert [
            (e.content, e.parent_tool_use_id)
            for e in harness.of(ToolResult)
            if e.turn_id == woken.turn_id
        ] == [("hi", AGENT_CALL)]
    finally:
        await harness.stop()
