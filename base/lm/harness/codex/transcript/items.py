from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import TypedDict

from base.lm.harness.codex.transcript.clock import iso_at
from base.lm.harness.codex.transcript.errors import MissingFieldError, UnknownValueError
from base.lm.harness.codex.transcript.scalars import opt_int, opt_text, text
from base.lm.harness.codex.transcript.shapes import (
    Obj,
    objects,
    opt_objects,
    opt_strings,
    strings,
)
from base.lm.harness.session.transcript.entries import (
    AgentText,
    Anomaly,
    AnomalyKind,
    AnyEntry,
    Compaction,
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
# the several entries one item produces (a call, its link, its result).
# meiva:link base/lm/harness/docs/transcript-projection.md

COMMAND_TOOL = "command"
SPAWN_TOOL = "spawnAgent"


class _Common(TypedDict):
    key: str
    order: Order
    record: str
    turn: str | None
    at: str | None


# meiva:note
# Where an item sits: the turn and record it belongs to, when it started and (if it has) finished, and
# its position in the caller's sequence.
@dataclass(frozen=True, slots=True)
class Where:
    turn: str
    record: str
    started_ms: int
    completed_ms: int | None
    seq: int


@dataclass(frozen=True, slots=True)
class _Place:
    id: str
    where: Where
    joined: bool

    @property
    def turn(self) -> str:
        return self.where.turn

    @property
    def started_ms(self) -> int:
        return self.where.started_ms

    @property
    def completed_ms(self) -> int | None:
        return self.where.completed_ms

    def common(self, kind: str, slot: int = 0, *, at_ms: int | None = None) -> _Common:
        return _Common(
            key=f"{kind}:{self.id}",
            order=Order(self.where.seq, slot),
            record=self.where.record,
            turn=self.turn,
            at=iso_at(self.started_ms if at_ms is None else at_ms),
        )

    def elapsed_ms(self) -> int | None:
        if self.completed_ms is None:
            return None
        return max(self.completed_ms - self.started_ms, 0)


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
        place = _Place(text(item, "id"), where, where.turn in input_turns)
        return handler(item, place)
    except (MissingFieldError, UnknownValueError) as error:
        order = Order(where.seq, 0)
        return [
            as_anomaly(error, subject=subject, order=order, record=where.record, turn=where.turn)
        ]


def _handler(kind: str) -> Callable[[Obj, _Place], list[AnyEntry]]:
    handler = _HANDLERS.get(kind)
    if handler is None:
        raise UnknownValueError(kind)
    return handler


def _user(item: Obj, place: _Place) -> list[AnyEntry]:
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
def _agent(item: Obj, place: _Place) -> list[AnyEntry]:
    return [AgentText(**place.common("agent"), text=text(item, "text"), message_ref=place.id)]


def _reasoning(item: Obj, place: _Place) -> list[AnyEntry]:
    readable = opt_strings(item, "summary") or opt_strings(item, "content")
    return [
        Reasoning(
            **place.common("reasoning"), text="\n".join(readable), duration_ms=place.elapsed_ms()
        )
    ]


def _compaction(_item: Obj, place: _Place) -> list[AnyEntry]:
    return [Compaction(**place.common("compaction"))]


_COMMAND_STATUS = {
    "completed": ToolStatus.OK,
    "failed": ToolStatus.ERROR,
    "declined": ToolStatus.DENIED,
}


def _command(item: Obj, place: _Place) -> list[AnyEntry]:
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


def _collab(item: Obj, place: _Place) -> list[AnyEntry]:
    tool = text(item, "tool")
    status = text(item, "status")
    receivers = strings(item, "receiverThreadIds")
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
            content=", ".join(receivers),
            status=_COLLAB_STATUS[status],
            duration_ms=place.elapsed_ms(),
        )
    )
    return entries


def _nicknames(item: Obj) -> dict[str, str]:
    labels: dict[str, str] = {}
    for agent in opt_objects(item, "receiverAgents"):
        thread, nickname = opt_text(agent, "threadId"), opt_text(agent, "agentNickname")
        if thread is not None and nickname is not None:
            labels[thread] = nickname
    return labels


# meiva:note
# The tools that are one finished item with no state in between (the rollout writes only the finished item): the
# call and its result together, under the tool names the live events use (`patch`, `view_image`, `web_search`).
# What goes into the call is paths and short text: a result that holds a picture or a long page is named, not copied.
RESULT_ITEM_CHARS = 2000


def _finished_pair(
    place: _Place, name: str, tool_input: dict[str, object], content: str, status: ToolStatus
) -> list[AnyEntry]:
    call = ToolCall(**place.common("call"), call_id=place.id, name=name, tool_input=tool_input)
    result = ToolResult(
        **place.common("result", 1, at_ms=place.completed_ms),
        call_id=place.id,
        content=content[:RESULT_ITEM_CHARS],
        status=status,
        duration_ms=place.elapsed_ms(),
    )
    return [call, result]


def _file_change(item: Obj, place: _Place) -> list[AnyEntry]:
    changes: list[dict[str, object]] = []
    for change in opt_objects(item, "changes"):
        path = opt_text(change, "path")
        if path is None:
            continue
        entry: dict[str, object] = {"path": path}
        if (kind := opt_text(change, "kind")) is not None:
            entry["kind"] = kind
        if (diff := opt_text(change, "diff")) is not None:
            entry["diff"] = diff
        changes.append(entry)
    status = text(item, "status")
    if status not in _COMMAND_STATUS:
        raise UnknownValueError(f"fileChange.status={status}")
    tool_input: dict[str, object] = {"paths": [str(change["path"]) for change in changes]}
    if changes:
        tool_input["changes"] = changes
    return _finished_pair(
        place, "patch", tool_input, opt_text(item, "output") or "", _COMMAND_STATUS[status]
    )


def _image_view(item: Obj, place: _Place) -> list[AnyEntry]:
    return _finished_pair(place, "view_image", {"path": text(item, "path")}, "", ToolStatus.OK)


def _web_search(item: Obj, place: _Place) -> list[AnyEntry]:
    query = text(item, "query")
    found = [
        f"{opt_text(result, 'title') or ''} {opt_text(result, 'url') or ''}".strip()
        for result in opt_objects(item, "results")
    ]
    return _finished_pair(
        place, "web_search", {"query": query}, "\n".join(found[:8]), ToolStatus.OK
    )


_HANDLERS: dict[str, Callable[[Obj, _Place], list[AnyEntry]]] = {
    "fileChange": _file_change,
    "imageView": _image_view,
    "webSearch": _web_search,
    "userMessage": _user,
    "agentMessage": _agent,
    "reasoning": _reasoning,
    "contextCompaction": _compaction,
    "commandExecution": _command,
    "collabAgentToolCall": _collab,
}
