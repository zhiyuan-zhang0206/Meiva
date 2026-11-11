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
# with `typeName` and `role`; `spawnStepIndex` is the output row of the LAST call of the planner row
# that started it, the same for every call of a parallel batch, so it is not used) and the call's
# own output row is found by the child's id; the child's transcript rows are read like the parent's: a `PLANNER_RESPONSE` row's `content` is what it
# said and its `tool_calls` are what it ran, the call at `tool_calls[k]` of the row at step `i` having
# its output in the `GENERIC` row at step `i + 1 + k`. Observed on agy 1.5.5; none of it is a published
# interface, so a file that is not this shape is skipped rather than guessed at.
# meiva:link base/lm/harness/docs/subagent-text.md


INVOKE_SUBAGENT = "invoke_subagent"


@dataclass(frozen=True, slots=True)
class Child:
    conversation_id: str
    label: str | None


def parse_child(raw: object) -> Child | None:
    if not is_object_dict(raw):
        return None
    conversation_id = raw.get("conversationId")
    if not isinstance(conversation_id, str) or not conversation_id:
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
    return Child(conversation_id=conversation_id, label=label)


# meiva:note
# Where the parent's `invoke_subagent` call that created `child` is. Its output row is the `GENERIC` row
# that names the child's id, and the call is `tool_calls[k]` of the nearest earlier `PLANNER_RESPONSE`
# row at step `i` with the output at step `i + 1 + k`, the same pairing as for any call. None until the
# output row is written.
def find_spawn(
    rows: Sequence[Mapping[str, object]], child: Child
) -> tuple[dict[str, object], str] | None:
    planner: Mapping[str, object] | None = None
    for row in rows:
        if row.get("type") == "PLANNER_RESPONSE" and is_object_list(row.get("tool_calls")):
            planner = row
        elif (content := _spawn_output(row, child)) is not None:
            return _spawn_arguments(planner, _step_index(row)), content
    return None


def _spawn_output(row: Mapping[str, object], child: Child) -> str | None:
    content = row.get("content")
    if row.get("type") != "GENERIC" or not isinstance(content, str):
        return None
    named = child.conversation_id in content and "Created the following subagent" in content
    return content if named else None


def _spawn_arguments(planner: Mapping[str, object] | None, output_step: int) -> dict[str, object]:
    calls = planner.get("tool_calls") if planner is not None else None
    if planner is None or not is_object_list(calls):
        return {}
    offset = output_step - _step_index(planner) - 1
    call = calls[offset] if 0 <= offset < len(calls) else None
    if not is_object_dict(call) or call.get("name") != INVOKE_SUBAGENT:
        return {}
    args = call.get("args")
    return dict(args) if is_object_dict(args) else {}


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
            name=INVOKE_SUBAGENT,
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
