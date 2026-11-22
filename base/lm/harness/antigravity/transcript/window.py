from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.lm.harness.antigravity.acp import UnrecognisedError, classify_system_message
from base.lm.harness.antigravity.reconciliation.queue_files import agy_home
from base.lm.harness.antigravity.transcript.text import background_notice, user_text, without_stamps
from base.lm.harness.session.transcript.entries import (
    AgentText,
    Anomaly,
    AnomalyKind,
    AnyEntry,
    Compaction,
    Failure,
    HumanInput,
    InputRow,
    Notice,
    Order,
    Reasoning,
    ToolCall,
    ToolResult,
    ToolStatus,
)
from base.lm.harness.session.transcript.record_file import FileRow, UnreadRows, stamp_of

CLI = "Antigravity"

_PLANNER_NON_DONE_STATUSES = frozenset(
    {"GENERATING", "RUNNING", "CANCELED", "CANCELLED", "ERROR", "FAILED"}
)


def wire_id(key: str) -> str:
    return key


def record_location(environ: Mapping[str, str]) -> str:
    return str(agy_home(None, environ) / ".gemini/antigravity-cli/brain")


def session_file(environ: Mapping[str, str], conversation_id: str) -> Path | None:
    path = (
        Path(record_location(environ))
        / conversation_id
        / ".system_generated/logs/transcript_full.jsonl"
    )
    return path if path.is_file() else None


@dataclass(frozen=True, slots=True)
class Call:
    id: str


# meiva:note
# File offsets are identities; step_index can repeat or go backwards. Results follow calls in file order,
# including through intervening task notices. Further results belong to the last call, not to a guessed step id.
# The old event writer is not a fallback for a row this reader cannot recognise. A row it cannot use is reported once
# per kind by the `UnreadRows` of the process that reads.
class EntryParser:
    pending_calls = False

    def __init__(self, unread: UnreadRows) -> None:
        self._calls: tuple[Call, ...] = ()
        self._result = 0
        self._unread = unread

    def entries(self, rows: Sequence[FileRow]) -> tuple[AnyEntry, ...]:
        found: list[AnyEntry] = []
        for row in rows:
            entries = self._row(row)
            for entry in entries:
                if isinstance(entry, Anomaly):
                    self._unread.report(CLI, entry)
                else:
                    found.append(entry)
        return tuple(found)

    def _row(self, file: FileRow) -> list[AnyEntry]:
        row = file.row
        kind, status = row.get("type"), row.get("status")
        key, order = f"agy:{file.offset}", Order(file.offset)
        stamp = stamp_of(row.get("created_at"))
        content = row.get("content")
        text = content if isinstance(content, str) else ""
        if stamp is None:
            return [_unknown(key, order, "created_at")]
        at = stamp.isoformat()
        if kind in ("GENERIC", "USER_INPUT", "SYSTEM_MESSAGE") and not isinstance(content, str):
            return [_unknown(key, order, f"{kind}.content")]
        if kind == "GENERIC":
            return self._generic(file, text, at)
        if kind == "PLANNER_RESPONSE" and status in _PLANNER_NON_DONE_STATUSES:
            return []
        if status != "DONE":
            return [_unknown(key, order, f"{kind}.status")]
        return self._done(file, text, stamp, at)

    def _done(self, file: FileRow, text: str, stamp: datetime, at: str) -> list[AnyEntry]:
        row = file.row
        kind = row.get("type")
        key, order = f"agy:{file.offset}", Order(file.offset)
        if kind == "USER_INPUT":
            return [HumanInput(key=key, order=order, at=at, wire_id=key, text=user_text(text))]
        if kind == "PLANNER_RESPONSE":
            return self._planner(file.offset, row, stamp, at)
        if kind == "SYSTEM_MESSAGE":
            try:
                notice = classify_system_message(text)
            except UnrecognisedError:
                return [_unknown(key, order, "SYSTEM_MESSAGE.envelope")]
            if notice is None:
                return []
            return [Notice(key=key, order=order, at=at, text=notice.text, cause=notice.cause)]
        if kind == "CHECKPOINT":
            return [Compaction(key=key, order=order, at=at)]
        if kind == "ERROR_MESSAGE":
            error = row.get("error")
            if not isinstance(error, str) or not error:
                return [_unknown(key, order, "ERROR_MESSAGE.error")]
            return [Failure(key=key, order=order, at=at, message=error)]
        return [_unknown(key, order, str(kind))]

    def _generic(self, file: FileRow, text: str, at: str) -> list[AnyEntry]:
        row = file.row
        status = row.get("status")
        key, order = f"agy:{file.offset}", Order(file.offset)
        if status not in ("DONE", "RUNNING", "ERROR", "INVALID"):
            return [_unknown(key, order, "GENERIC.status")]
        if not self._calls:
            # meiva:note
            # The window has not reached the call yet. Keep its output visible while the common
            # window reader looks back for context; a whole record never starts with a result.
            call = Call(f"agy:before:{file.offset}")
        else:
            call = self._calls[min(self._result, len(self._calls) - 1)]
        self._result += 1
        error = row.get("error")
        shown = background_notice(text) if status == "RUNNING" else without_stamps(text)
        if status in ("ERROR", "INVALID") and isinstance(error, str) and error not in shown:
            shown = f"{shown}\n{error}".strip()
        return [
            ToolResult(
                key=key,
                order=order,
                at=at,
                call_id=call.id,
                content=shown,
                status=ToolStatus.ERROR if status in ("ERROR", "INVALID") else ToolStatus.OK,
            )
        ]

    def _planner(
        self, offset: int, row: Mapping[str, object], stamp: datetime, at: str
    ) -> list[AnyEntry]:
        key = f"agy:{offset}"
        found: list[AnyEntry] = []
        thinking, text = row.get("thinking"), row.get("content")
        if isinstance(thinking, str) and thinking.strip():
            # meiva:note
            # Antigravity's PLANNER_RESPONSE.created_at is the step start; advance Reasoning.at by
            # thinkingDuration when known so committed reasoning items uniformly carry their
            # completion time (`start = created_at - reasoning_ms`, `end = created_at`).
            duration_ms = _reasoning_ms(row)
            reasoning_at = (
                (stamp + timedelta(milliseconds=duration_ms)).isoformat()
                if duration_ms is not None
                else at
            )
            found.append(
                Reasoning(
                    key=f"{key}:thinking",
                    order=Order(offset),
                    at=reasoning_at,
                    text=thinking.strip(),
                    duration_ms=duration_ms,
                )
            )
        if isinstance(text, str) and text.strip():
            found.append(AgentText(key=key, order=Order(offset, 1), at=at, text=text.strip()))
        raw = row.get("tool_calls", [])
        if not is_object_list(raw):
            return [*found, _unknown(key, Order(offset), "PLANNER_RESPONSE.tool_calls")]
        calls: list[Call] = []
        for index, call in enumerate(raw):
            if not is_object_dict(call):
                return [*found, _unknown(key, Order(offset), "PLANNER_RESPONSE.tool_call")]
            name, args = call.get("name"), call.get("args")
            if not isinstance(name, str) or not is_object_dict(args):
                return [*found, _unknown(key, Order(offset), "PLANNER_RESPONSE.tool_call")]
            call_id = f"{key}:call:{index}"
            calls.append(Call(call_id))
            found.append(
                ToolCall(
                    key=call_id,
                    order=Order(offset, index + 2),
                    at=at,
                    call_id=call_id,
                    name=name,
                    tool_input=args,
                )
            )
        self._calls, self._result = tuple(calls), 0
        return found


def _unknown(key: str, order: Order, detail: str) -> Anomaly:
    return Anomaly(
        key=f"{key}:unknown", order=order, anomaly=AnomalyKind.UNKNOWN_TYPE, detail=detail
    )


def input_wire_ids(rows: Sequence[FileRow]) -> dict[str, InputRow]:
    return {
        f"agy:{r.offset}": InputRow(stamp, user_text(content))
        for r in rows
        if r.row.get("type") == "USER_INPUT"
        and isinstance((content := r.row.get("content")), str)
        and (stamp := stamp_of(r.row.get("created_at"))) is not None
    }


def _reasoning_ms(row: Mapping[str, object]) -> int | None:
    value = row.get("meiva_reasoning_ms")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
