from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from base.lm.harness.antigravity.subagent_files import SubagentReader
from base.lm.harness.antigravity.subagents import (
    Child,
    ChildCursor,
    advance,
    find_spawn,
    parse_child,
)
from base.lm.harness.antigravity.tests.support import say, start
from base.lm.harness.delivery.tests.sent import send
from base.lm.harness.events import SubagentText, ToolResult, ToolUse, TurnCompleted

FIXTURE = Path(__file__).with_name("fixtures") / "subagent_conversation.json"
RECORDED = cast(dict[str, Any], json.loads(FIXTURE.read_text(encoding="utf-8")))
CHILD = "child-conv"


def place(
    home: Path, parent: str, *, child_rows: int | None = None, parent_rows: int | None = None
) -> None:
    # meiva:note
    # The recorded files of one sub-agent run laid out where agy writes them under a home. `child_rows`
    # and `parent_rows` write only that many rows, the state of a run still in progress.
    # meiva:link base/lm/harness/docs/subagent-text.md
    brain = home / ".gemini" / "antigravity-cli" / "brain"
    parent_system = brain / parent / ".system_generated"
    (parent_system / "subagents").mkdir(parents=True, exist_ok=True)
    (parent_system / "logs").mkdir(parents=True, exist_ok=True)
    (parent_system / "subagents" / f"{CHILD}.json").write_text(
        json.dumps(RECORDED["parent_descriptor"]), encoding="utf-8"
    )
    (parent_system / "logs" / "transcript_full.jsonl").write_text(
        _lines(RECORDED["parent_rows"][:parent_rows]), encoding="utf-8"
    )
    child_logs = brain / CHILD / ".system_generated" / "logs"
    child_logs.mkdir(parents=True, exist_ok=True)
    (child_logs / "transcript_full.jsonl").write_text(
        _lines(RECORDED["child_rows"][:child_rows]), encoding="utf-8"
    )


def _lines(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(row) + "\n" for row in rows)


def test_a_descriptor_names_the_child_its_label_and_the_spawn_step() -> None:
    assert parse_child(RECORDED["parent_descriptor"]) == Child(CHILD, "Task Runner")
    assert parse_child({"conversationId": ""}) is None
    assert parse_child({"spawnStepIndex": 2}) is None
    assert parse_child([]) is None


def test_a_childs_rows_become_its_text_and_its_tool_calls() -> None:
    child = Child(CHILD, "Task Runner")
    events, cursor = advance(RECORDED["child_rows"], child, ChildCursor(), turn_id="turn-1")
    assert [e for e in events if isinstance(e, SubagentText)] == [
        SubagentText("turn-1", CHILD, "Task Runner", "ALPHA.\nShell output:\nhi\nBETA.")
    ]
    calls = [e for e in events if isinstance(e, ToolUse)]
    assert [(e.name, e.tool_use_id, e.parent_tool_use_id) for e in calls] == [
        ("run_command", f"{CHILD}#2", CHILD),
        ("send_message", f"{CHILD}#4", CHILD),
    ]
    assert calls[0].tool_input["CommandLine"] == "echo hi"
    results = [e for e in events if isinstance(e, ToolResult)]
    assert [(e.tool_use_id, e.parent_tool_use_id) for e in results] == [
        (f"{CHILD}#2", CHILD),
        (f"{CHILD}#4", CHILD),
    ]
    assert results[0].content == "Command exited with code 0.\n\nCommand output:\nhi"
    assert cursor.seen == len(RECORDED["child_rows"]) and cursor.open_calls == frozenset()


def test_a_childs_rows_are_reported_once_as_the_transcript_grows() -> None:
    child = Child(CHILD, "Task Runner")
    rows = RECORDED["child_rows"]
    first, cursor = advance(rows[:2], child, ChildCursor(), turn_id="turn-1")
    assert [type(e) for e in first] == [ToolUse], "the call shows before its output exists"
    assert cursor.open_calls == frozenset({f"{CHILD}#2"})
    rest, cursor = advance(rows, child, cursor, turn_id="turn-2")
    assert all(
        isinstance(e, ToolUse | ToolResult | SubagentText) and e.turn_id == "turn-2" for e in rest
    )
    assert [type(e) for e in rest] == [ToolResult, ToolUse, ToolResult, SubagentText]
    again, _ = advance(rows, child, cursor, turn_id="turn-3")
    assert again == ()


def test_a_poll_reports_the_spawn_call_then_what_the_child_did(tmp_path: Path) -> None:
    place(tmp_path, "parent")
    reader = SubagentReader(tmp_path)
    events = reader.poll("parent", "turn-1")
    assert [type(e) for e in events[:2]] == [ToolUse, ToolResult]
    [spawn] = [e for e in events if isinstance(e, ToolUse) and e.parent_tool_use_id is None]
    assert (spawn.name, spawn.tool_use_id, spawn.turn_id) == ("invoke_subagent", CHILD, "turn-1")
    assert str(spawn.tool_input["Prompt"]).startswith("Say ALPHA")
    [spawned] = [e for e in events if isinstance(e, ToolResult) and e.tool_use_id == CHILD]
    assert spawned.content.startswith("Created the following subagent")
    assert len([e for e in events if isinstance(e, SubagentText)]) == 1
    assert reader.poll("parent", "turn-1") == (), "nothing is reported twice"


def test_a_child_whose_spawn_output_is_not_written_yet_waits(tmp_path: Path) -> None:
    place(tmp_path, "parent", parent_rows=2)
    reader = SubagentReader(tmp_path)
    assert reader.poll("parent", "turn-1") == ()
    place(tmp_path, "parent")
    assert reader.poll("parent", "turn-1") != ()


def test_a_resumed_conversations_old_children_report_only_what_they_write_next(
    tmp_path: Path,
) -> None:
    place(tmp_path, "parent", child_rows=2)
    reader = SubagentReader(tmp_path)
    reader.adopt("parent")
    assert reader.poll("parent", "turn-1") == ()
    place(tmp_path, "parent")
    events = reader.poll("parent", "turn-2")
    assert [type(e) for e in events] == [ToolUse, ToolResult, SubagentText]


def test_an_unreadable_descriptor_is_left_out_and_does_not_stop_the_others(tmp_path: Path) -> None:
    place(tmp_path, "parent")
    broken = tmp_path / ".gemini" / "antigravity-cli" / "brain" / "parent" / ".system_generated"
    (broken / "subagents" / "aaa-broken.json").write_text("{not json", encoding="utf-8")
    events = SubagentReader(tmp_path).poll("parent", "turn-1")
    assert [e for e in events if isinstance(e, SubagentText)] != []


async def test_a_turn_shows_what_its_subagent_wrote(tmp_path: Path) -> None:
    harness = await start(tmp_path, turns=[say("DONE")])
    try:
        place(tmp_path / "home", "conv-fake")
        turn = await send(harness, "delegate")
        await turn.outcome()
        completed = harness.of(TurnCompleted)[0]
        texts = harness.of(SubagentText)
        assert [(e.tool_use_id, e.agent, e.turn_id) for e in texts] == [
            (CHILD, "Task Runner", turn.turn_id)
        ]
        names = [(e.name, e.parent_tool_use_id) for e in harness.of(ToolUse)]
        assert names == [
            ("invoke_subagent", None),
            ("run_command", CHILD),
            ("send_message", CHILD),
        ]
        order: list[type] = [type(e) for e in harness.events]
        assert order.index(SubagentText) < order.index(TurnCompleted)
        assert completed.text == "DONE", "the sub-agent's words are not the turn's reply"
    finally:
        await harness.stop()


PARALLEL = cast(
    dict[str, Any],
    json.loads(FIXTURE.with_name("subagent_parallel.json").read_text(encoding="utf-8")),
)


def place_parallel(home: Path) -> None:
    brain = home / ".gemini" / "antigravity-cli" / "brain"
    system = brain / "parent-conv" / ".system_generated"
    (system / "subagents").mkdir(parents=True)
    (system / "logs").mkdir()
    for descriptor in PARALLEL["parent_descriptors"]:
        path = system / "subagents" / f"{descriptor['conversationId']}.json"
        path.write_text(json.dumps(descriptor), encoding="utf-8")
    (system / "logs" / "transcript_full.jsonl").write_text(
        _lines(PARALLEL["parent_rows"]), encoding="utf-8"
    )
    for child, rows in (("child-a", "child_a_rows"), ("child-b", "child_b_rows")):
        logs = brain / child / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        (logs / "transcript_full.jsonl").write_text(_lines(PARALLEL[rows]), encoding="utf-8")


# meiva:note
# Recorded on agy 1.5.5: both descriptors say spawnStepIndex 3 (the last call's output row), so the
# first child's call must be found by its id, not by that step.
def test_parallel_calls_each_get_their_own_arguments_and_output(tmp_path: Path) -> None:
    place_parallel(tmp_path)
    events = SubagentReader(tmp_path).poll("parent-conv", "turn-1")
    spawns = [e for e in events if isinstance(e, ToolUse) and e.parent_tool_use_id is None]
    assert [e.tool_use_id for e in spawns] == ["child-a", "child-b"]
    assert "AAA-111" in str(spawns[0].tool_input["Prompt"])
    assert "BBB-222" in str(spawns[1].tool_input["Prompt"])
    outputs = [e for e in events if isinstance(e, ToolResult) and e.parent_tool_use_id is None]
    assert [(e.tool_use_id, f'"{e.tool_use_id}"' in e.content) for e in outputs] == [
        ("child-a", True),
        ("child-b", True),
    ]
    texts = {e.tool_use_id: e.agent for e in events if isinstance(e, SubagentText)}
    assert texts == {"child-a": "Worker A", "child-b": "Worker B"}


def test_a_spawn_whose_output_row_is_missing_is_not_found() -> None:
    child = Child("child-b", "Worker B")
    assert find_spawn(PARALLEL["parent_rows"][:3], child) is None
    found = find_spawn(PARALLEL["parent_rows"], child)
    assert found is not None and "BBB-222" in str(found[0]["Prompt"])
