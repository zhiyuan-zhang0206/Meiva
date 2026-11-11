from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.lm.harness.events import (
    BridgeEvent,
    SubagentText,
    ToolResult,
    ToolUse,
)

# meiva:note
# `invoke_subagent` creates a sibling conversation: its own `brain/<id>/` directory with a transcript
# in the same shape as the parent's. The stream-json frames show nothing of it (no step for the call,
# none for the child), so everything here is read from files. The parent's
# `.system_generated/subagents/<child>.json` names each child (`conversationId`, `subagentDescriptor`
# with `typeName` and `role`, `spawnStepIndex`: the parent step that holds the call's output), and the
# child's transcript rows are read like the parent's: a `PLANNER_RESPONSE` row's `content` is what it
# said and its `tool_calls` are what it ran, the call at `tool_calls[k]` of the row at step `i` having
# its output in the `GENERIC` row at step `i + 1 + k`. Observed on agy 1.5.5; none of it is a published
# interface, so a file that is not this shape is skipped rather than guessed at.
# meiva:link base/lm/harness/docs/subagent-text.md


@dataclass(frozen=True, slots=True)
class Child:
    conversation_id: str
    label: str | None
    spawn_step: int


def parse_child(raw: object) -> Child | None:
    if not is_object_dict(raw):
        return None
    conversation_id = raw.get("conversationId")
    spawn_step = raw.get("spawnStepIndex")
    if (
        not isinstance(conversation_id, str)
        or not conversation_id
        or isinstance(spawn_step, bool)
        or not isinstance(spawn_step, int)
    ):
        return None
    descriptor = raw.get("subagentDescriptor")
    named = descriptor if is_object_dict(descriptor) else {}
    label = next(
        (
            value
            for key in ("role", "typeName")
            if isinstance(value := named.get(key), str) and value
        ),
        None,
    )
    return Child(conversation_id=conversation_id, label=label, spawn_step=spawn_step)


# meiva:note
# How far a child's transcript has been shown. `seen` is the number of rows consumed; `open_calls` are the
# tool-call step ids whose output row has not arrived yet; `announced` says the parent's own
# `invoke_subagent` call has been written. The call's id is the child's conversation id: stable across
# turns and restarts, which is what lets a later turn (or process) attach the child's activity to the
# call that started it.
@dataclass(frozen=True, slots=True)
class ChildCursor:
    seen: int = 0
    open_calls: frozenset[str] = frozenset()
    announced: bool = False


def spawn_events(
    child: Child, *, turn_id: str, arguments: Mapping[str, object], output: str
) -> tuple[BridgeEvent, ...]:
    return (
        ToolUse(
            turn_id=turn_id,
            tool_use_id=child.conversation_id,
            name="invoke_subagent",
            tool_input=dict(arguments),
        ),
        ToolResult(
            turn_id=turn_id,
            tool_use_id=child.conversation_id,
            is_error=False,
            content=_without_stamps(output),
        ),
    )


def advance(
    rows: Sequence[Mapping[str, object]], child: Child, cursor: ChildCursor, *, turn_id: str
) -> tuple[tuple[BridgeEvent, ...], ChildCursor]:
    events: list[BridgeEvent] = []
    open_calls = set(cursor.open_calls)
    for row in rows[cursor.seen :]:
        step = _step_index(row)
        kind = row.get("type")
        if kind == "PLANNER_RESPONSE":
            events.extend(_planner_events(row, step, child, turn_id, open_calls))
        elif kind == "GENERIC" and (call_id := f"{child.conversation_id}#{step}") in open_calls:
            open_calls.discard(call_id)
            events.append(
                ToolResult(
                    turn_id=turn_id,
                    tool_use_id=call_id,
                    is_error=False,
                    content=_without_stamps(row.get("content")),
                    parent_tool_use_id=child.conversation_id,
                )
            )
    advanced = ChildCursor(
        seen=max(len(rows), cursor.seen),
        open_calls=frozenset(open_calls),
        announced=cursor.announced,
    )
    return tuple(events), advanced


def _planner_events(
    row: Mapping[str, object],
    step: int,
    child: Child,
    turn_id: str,
    open_calls: set[str],
) -> list[BridgeEvent]:
    events: list[BridgeEvent] = []
    content = row.get("content")
    if isinstance(content, str) and content.strip():
        events.append(
            SubagentText(
                turn_id=turn_id,
                tool_use_id=child.conversation_id,
                agent=child.label,
                text=content.strip(),
            )
        )
    calls = row.get("tool_calls")
    for offset, call in enumerate(calls if is_object_list(calls) else []):
        if not is_object_dict(call) or not isinstance(call.get("name"), str):
            continue
        args = call.get("args")
        call_id = f"{child.conversation_id}#{step + 1 + offset}"
        open_calls.add(call_id)
        events.append(
            ToolUse(
                turn_id=turn_id,
                tool_use_id=call_id,
                name=str(call["name"]),
                tool_input=dict(args) if is_object_dict(args) else {},
                parent_tool_use_id=child.conversation_id,
            )
        )
    return events


def _step_index(row: Mapping[str, object]) -> int:
    value = row.get("step_index")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


# meiva:note
# A tool's output row opens with the CLI's own `Created At:` / `Completed At:` stamps, which say when the
# call ran and nothing about what it did.
def _without_stamps(content: object) -> str:
    if not isinstance(content, str):
        return ""
    lines = content.splitlines()
    start = 0
    while start < len(lines) and lines[start].startswith(("Created At:", "Completed At:")):
        start += 1
    return "\n".join(lines[start:]).strip()
