from __future__ import annotations

from collections.abc import Callable, Collection

from base.host.boundary.json_values import is_object_dict
from base.lm.harness.codex.transcript.clock import iso_at
from base.lm.harness.codex.transcript.errors import MissingFieldError, UnknownValueError
from base.lm.harness.codex.transcript.place import Place, Where
from base.lm.harness.codex.transcript.scalars import opt_int, opt_text, text
from base.lm.harness.codex.transcript.shapes import (
    Obj,
    objects,
    opt_objects,
    opt_strings,
)
from base.lm.harness.codex.transcript.tools import HANDLERS as TOOL_HANDLERS
from base.lm.harness.session.transcript.entries import (
    AgentText,
    Anomaly,
    AnomalyKind,
    AnyEntry,
    Compaction,
    Failure,
    HumanInput,
    Interrupt,
    Order,
    Reasoning,
    SubagentLink,
    ToolCall,
    ToolResult,
    ToolStatus,
    TurnEnd,
)

# meiva:note
# One thread item in the protocol's own shape (`thread/items/list`, `thread/read`) read into entries.
# The rollout's `event_msg/item_completed` is converted to this shape first (`rollout.py`), so a
# history item and its rollout twin take the same path and get the same keys: the key of a row is its
# kind and the item id, never its position. `Order.seq` is the caller's position and `Order.sub` numbers
# the several entries one item produces (a call, its link, its result). The tool-like items (edits, MCP and
# dynamic tools, web search, plans...) are in `tools.py`.
# meiva:link base/lm/harness/docs/transcript-projection.md

COMMAND_TOOL = "command"
SPAWN_TOOL = "spawnAgent"


def as_anomaly(
    error: MissingFieldError | UnknownValueError,
    *,
    subject: str,
    order: Order,
    record: str,
    turn: str | None,
) -> Anomaly:
    if isinstance(error, MissingFieldError):
        kind, detail = AnomalyKind.MISSING_FIELD, error.field
    else:
        kind, detail = AnomalyKind.UNKNOWN_TYPE, error.detail
    return Anomaly(
        key=f"anomaly:{kind.value}:{subject}:{detail}",
        order=order,
        record=record,
        turn=turn,
        anomaly=kind,
        detail=detail,
    )


# meiva:note
# A turn's own boundary rows. The history reads them from a turn's `status`, the rollout from
# `task_complete` / `turn_aborted`; both give the same key per turn, so whichever arrives second adds
# nothing.
def turn_end_entry(turn: str, at_ms: int | None, seq: int, record: str) -> TurnEnd:
    return TurnEnd(
        key=f"turn-end:{turn}",
        order=Order(seq, 0),
        record=record,
        turn=turn,
        at=None if at_ms is None else iso_at(at_ms),
    )


def failure_entry(turn: str, message: str, at_ms: int | None, seq: int, record: str) -> Failure:
    return Failure(
        key=f"failure:{turn}",
        order=Order(seq, 0),
        record=record,
        turn=turn,
        at=None if at_ms is None else iso_at(at_ms),
        message=message,
    )


def interrupt_entry(turn: str, at_ms: int | None, seq: int, record: str) -> Interrupt:
    return Interrupt(
        key=f"interrupt:{turn}",
        order=Order(seq, 0),
        record=record,
        turn=turn,
        at=None if at_ms is None else iso_at(at_ms),
    )


def remember_inputs(entries: list[AnyEntry], input_turns: set[str]) -> None:
    for entry in entries:
        if isinstance(entry, HumanInput) and entry.turn is not None:
            input_turns.add(entry.turn)


# meiva:note
# A steer is a user message taken into a turn that already has one: it belongs to the same turn as the
# original, and nothing in the item says so, so the rule is positional. `input_turns` holds the turns
# whose first input the caller has already read (a page of `thread/items/list` may start in the middle
# of a turn, so the caller carries it across pages).
def item_entries(item: Obj, where: Where, input_turns: Collection[str]) -> list[AnyEntry]:
    subject = opt_text(item, "id") or "?"
    try:
        handler = _handler(text(item, "type"))
        place = Place(text(item, "id"), where, where.turn in input_turns)
        return handler(item, place)
    except (MissingFieldError, UnknownValueError) as error:
        order = Order(where.seq, 0)
        return [
            as_anomaly(error, subject=subject, order=order, record=where.record, turn=where.turn)
        ]


def _handler(kind: str) -> Callable[[Obj, Place], list[AnyEntry]]:
    handler = _HANDLERS.get(kind)
    if handler is None:
        raise UnknownValueError(kind)
    return handler


def _user(item: Obj, place: Place) -> list[AnyEntry]:
    parts = [
        text(part, "text") for part in objects(item, "content") if opt_text(part, "type") == "text"
    ]
    return [
        HumanInput(
            **place.common("user"),
            wire_id=opt_text(item, "clientId"),
            text="\n".join(parts),
            joined_running_turn=place.joined,
        )
    ]


# meiva:note
# `AgentText` has no phase, so Codex's `commentary` / `final_answer` distinction is dropped.
def _agent(item: Obj, place: Place) -> list[AnyEntry]:
    return [AgentText(**place.common("agent"), text=text(item, "text"), message_ref=place.id)]


def _reasoning(item: Obj, place: Place) -> list[AnyEntry]:
    readable = opt_strings(item, "summary") or opt_strings(item, "content")
    return [
        Reasoning(
            **place.common("reasoning"), text="\n".join(readable), duration_ms=place.elapsed_ms()
        )
    ]


def _compaction(_item: Obj, place: Place) -> list[AnyEntry]:
    return [Compaction(**place.common("compaction"))]


_COMMAND_STATUS = {
    "completed": ToolStatus.OK,
    "failed": ToolStatus.ERROR,
    "declined": ToolStatus.DENIED,
}


def _command(item: Obj, place: Place) -> list[AnyEntry]:
    command = text(item, "command")
    status = text(item, "status")
    tool_input: dict[str, object] = {"command": command}
    if (cwd := opt_text(item, "cwd")) is not None:
        tool_input["cwd"] = cwd
    call = ToolCall(
        **place.common("call"), call_id=place.id, name=COMMAND_TOOL, tool_input=tool_input
    )
    if status == "inProgress":
        return [call]
    if status not in _COMMAND_STATUS:
        raise UnknownValueError(f"commandExecution.status={status}")
    exit_code = opt_int(item, "exitCode")
    failed = status == "completed" and exit_code not in (None, 0)
    # meiva:note
    # Call and result are yielded together, with the turn the command started in: a command that
    # completes after its turn ended arrives late and is appended when it does.
    duration = opt_int(item, "durationMs")
    result = ToolResult(
        **place.common("result", 1, at_ms=place.completed_ms),
        call_id=place.id,
        content=opt_text(item, "aggregatedOutput") or "",
        status=ToolStatus.ERROR if failed else _COMMAND_STATUS[status],
        duration_ms=place.elapsed_ms() if duration is None else duration,
    )
    return [call, result]


_COLLAB_STATUS = {
    "completed": ToolStatus.OK,
    "failed": ToolStatus.ERROR,
    "interrupted": ToolStatus.ABORTED,
}


def _collab(item: Obj, place: Place) -> list[AnyEntry]:
    tool = text(item, "tool")
    status = text(item, "status")
    receivers = opt_strings(item, "receiverThreadIds")
    tool_input: dict[str, object] = {"receivers": receivers}
    for field in ("prompt", "model"):
        if (value := opt_text(item, field)) is not None:
            tool_input[field] = value
    entries: list[AnyEntry] = [
        ToolCall(**place.common("call"), call_id=place.id, name=tool, tool_input=tool_input)
    ]
    if tool == SPAWN_TOOL:
        labels = _nicknames(item)
        entries.extend(
            SubagentLink(
                **place.common(f"link:{child}", 1),
                call_id=place.id,
                child_record=child,
                label=labels.get(child),
            )
            for child in receivers
        )
    if status == "inProgress":
        return entries
    if status not in _COLLAB_STATUS:
        raise UnknownValueError(f"collabAgentToolCall.status={status}")
    entries.append(
        ToolResult(
            **place.common("result", 2, at_ms=place.completed_ms),
            call_id=place.id,
            content=_collab_report(item) or ", ".join(receivers),
            status=_COLLAB_STATUS[status],
            duration_ms=place.elapsed_ms(),
        )
    )
    return entries


# meiva:note
# What a collab call hands back to the model, when it carries words: the message each receiver's state holds (a
# `wait` returns the children's last messages this way). A spawn's states carry none and its result stays the
# receiver ids.
def _collab_report(item: Obj) -> str:
    states = item.get("agentsStates")
    if not is_object_dict(states):
        return ""
    lines: list[str] = []
    for thread, state in states.items():
        message = opt_text(state, "message") if is_object_dict(state) else None
        if message:
            lines.append(f"{thread}: {message}")
    return "\n".join(lines)


def _nicknames(item: Obj) -> dict[str, str]:
    labels: dict[str, str] = {}
    for agent in opt_objects(item, "receiverAgents"):
        thread, nickname = opt_text(agent, "threadId"), opt_text(agent, "agentNickname")
        if thread is not None and nickname is not None:
            labels[thread] = nickname
    return labels


_HANDLERS: dict[str, Callable[[Obj, Place], list[AnyEntry]]] = {
    "userMessage": _user,
    "agentMessage": _agent,
    "reasoning": _reasoning,
    "contextCompaction": _compaction,
    "commandExecution": _command,
    "collabAgentToolCall": _collab,
    **TOOL_HANDLERS,
}
