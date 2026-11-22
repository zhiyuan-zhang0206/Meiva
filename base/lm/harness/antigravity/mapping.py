from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.jsonl import read_jsonl_rows
from base.host.env.harness_binaries import resolve_user_home
from base.lm.harness.antigravity.acp import content_text, input_marker, received_markers
from base.lm.harness.events import (
    AssistantText,
    HarnessCause,
    TurnCompleted,
    TurnOrigin,
    TurnStarted,
    Usage,
)
from base.lm.harness.session.transcript.record_file import RecordFile


def text_update(text: str) -> dict[str, object]:
    return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}


def transcript_path(conversation_id: str, *, home: Path | None = None) -> Path | None:
    log_dir = (
        resolve_user_home(home)
        / ".gemini"
        / "antigravity-cli"
        / "brain"
        / conversation_id
        / ".system_generated"
        / "logs"
    )
    for filename in ("transcript_full.jsonl", "transcript.jsonl"):
        path = log_dir / filename
        if path.is_file():
            return path
    return None


def transcript_rows(conversation_id: str, *, home: Path | None = None) -> list[dict[str, object]]:
    path = transcript_path(conversation_id, home=home)
    if path is None:
        return []
    try:
        return read_jsonl_rows(path)
    except (OSError, ValueError):
        return []


@dataclass(frozen=True, slots=True)
class TranscriptSnapshot:
    rows: list[dict[str, object]]
    offsets: tuple[int, ...]

    @classmethod
    def read(cls, path: Path | None) -> TranscriptSnapshot:
        if path is None:
            return cls([], ())
        try:
            records = RecordFile(path).rows_after(0).rows
        except OSError:
            return cls([], ())
        return cls(
            [dict(record.row) for record in records], tuple(record.offset for record in records)
        )


@dataclass(frozen=True, slots=True)
class PlannerRow:
    index: int
    row: dict[str, object]
    input_key: str | None
    notice: BackgroundNotice | None

    @property
    def final(self) -> bool:
        text = self.row.get("content")
        return isinstance(text, str) and bool(text.strip()) and not self.row.get("tool_calls")


def planner_rows(rows: Sequence[dict[str, object]], keys: Sequence[str]) -> Iterator[PlannerRow]:
    marks = {input_marker(key): key for key in keys}
    foreground: str | None = None
    background: BackgroundNotice | None = None
    for index, row in enumerate(rows):
        kind = row.get("type")
        if kind == "USER_INPUT":
            foreground = next(
                (marks[mark] for mark in received_markers((row,)) if mark in marks), None
            )
            background = None
        elif kind == "SYSTEM_MESSAGE" and (notice := _background_notice(row, index)) is not None:
            background = notice
        elif kind == "PLANNER_RESPONSE" and row.get("status") == "DONE":
            yield PlannerRow(index, row, foreground, background)


_SENDER = re.compile(r"^\[Message\] (?:timestamp=\S+ )?sender=(\S+)([^\n]*)", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class BackgroundNotice:
    sender: str
    prompt: str
    index: int

    def is_task_for(self, session_id: str | None) -> bool:
        return bool(session_id and self.sender.startswith(session_id + "/task-"))


def _background_notice(row: dict[str, object], index: int) -> BackgroundNotice | None:
    content = row.get("content")
    match = _SENDER.search(content) if isinstance(content, str) else None
    if match is None or match[1] == "system":
        return None
    _, separator, prompt = match[2].partition(" content=")
    if separator and isinstance(content, str):
        prompt += content[match.end() :].partition("</SYSTEM_MESSAGE>")[0]
    return BackgroundNotice(match[1], prompt.strip(), index)


@dataclass(frozen=True, slots=True)
class ProjectedFrame:
    frame: dict[str, object]
    input_key: str | None
    notice: BackgroundNotice | None
    reply: str = ""
    text_finished: bool = False
    reply_index: int | None = None

    def timer_notice(self, session_id: str | None) -> BackgroundNotice | None:
        if self.reply and self.notice is not None and self.notice.is_task_for(session_id):
            return self.notice
        return None


Frame = dict[str, object]
Evidence = tuple[int, str, int]


@dataclass(frozen=True, slots=True)
class ForegroundBoundary:
    next_row: int
    accounted: bool
    cancelled: bool

    def excludes(self, step: PlannerRow, notice: BackgroundNotice) -> bool:
        tokens = step.row.get("input_tokens")
        if notice.index >= self.next_row:
            return True
        if step.index < self.next_row:
            return False
        if self.cancelled:
            return step.final
        return self.accounted and type(tokens) is int and tokens > 0


@dataclass(slots=True)
class OutputProjection:
    offset: int
    keys: list[str] = field(default_factory=list[str])
    pending: deque[Frame] = field(default_factory=deque[Frame])
    confirmed: set[int] = field(default_factory=set[int])
    excluded: set[int] = field(default_factory=set[int])
    _answered_notices: set[int] = field(default_factory=set[int])
    _boundaries: dict[str, ForegroundBoundary] = field(
        default_factory=dict[str, ForegroundBoundary]
    )
    seen: set[Evidence] = field(default_factory=set[Evidence])
    _tool_owners: dict[str, ProjectedFrame] = field(default_factory=dict[str, ProjectedFrame])

    def owns(self, step: PlannerRow) -> bool:
        return step.input_key is not None and (step.notice is None or step.index in self.confirmed)

    def steps(self, rows: Sequence[Frame]) -> tuple[PlannerRow, ...]:
        return tuple(planner_rows(rows[self.offset :], self.keys))

    def confirm_span(self, span: Sequence[PlannerRow], candidates: Sequence[PlannerRow]) -> None:
        selected = {step.index for step in span}
        self.confirmed.update(selected)
        self.excluded.update(step.index for step in candidates if step.index not in selected)

    def close_foreground(
        self, keys: Sequence[str], row_count: int, *, accounted: bool, cancelled: bool = False
    ) -> None:
        boundary = ForegroundBoundary(row_count - self.offset, accounted, cancelled)
        self._boundaries.update(dict.fromkeys(keys, boundary))

    def _route(self, step: PlannerRow) -> str | BackgroundNotice | None:
        if self.owns(step):
            return step.input_key
        notice = step.notice
        if notice is None:
            return None
        boundary = self._boundaries.get(step.input_key) if step.input_key is not None else None
        if step.input_key is None or step.index in self.excluded:
            return notice
        return notice if boundary is not None and boundary.excludes(step, notice) else None

    def take_background_reply(
        self, transcript: TranscriptSnapshot, item: ProjectedFrame, session_id: str | None
    ) -> NativeReply | None:
        notice, final = item.timer_notice(session_id), item.reply_index
        if notice is None or final is None:
            return None
        if notice.index in self._answered_notices:
            return None
        steps = tuple(
            step
            for step in self.steps(transcript.rows)
            if notice.index < step.index <= final and self._route(step) == notice
        )
        if not steps:
            return None
        self._answered_notices.add(notice.index)
        return NativeReply(notice, transcript.offsets[self.offset + notice.index], steps)

    def expects_tool(self, tool_id: object) -> bool:
        return isinstance(tool_id, str) and (
            tool_id in self._tool_owners
            or any(item.get("toolCallId") == tool_id for item in self.pending)
        )

    def project(self, rows: Sequence[Frame]) -> Iterator[ProjectedFrame]:
        steps = self.steps(rows)
        while self.pending:
            count = len(self.pending)
            yield from self._project_pass(steps, interleave=False)
            if len(self.pending) != count:
                continue
            yield from self._project_pass(steps, interleave=True)
            if len(self.pending) == count:
                return

    def _project_pass(
        self, steps: Sequence[PlannerRow], *, interleave: bool
    ) -> Iterator[ProjectedFrame]:
        consumed: set[int] = set()
        frames = list(self.pending)
        for index in range(len(frames)):
            if index in consumed:
                continue
            match = self._match(index, frames, consumed, steps, interleave=interleave)
            if match is not None:
                event, indices = match
                consumed.update(indices)
                yield event
        self.pending = deque(frame for i, frame in enumerate(frames) if i not in consumed)

    def _match(
        self,
        index: int,
        frames: Sequence[Frame],
        consumed: set[int],
        steps: Sequence[PlannerRow],
        *,
        interleave: bool,
    ) -> tuple[ProjectedFrame, tuple[int, ...]] | None:
        frame = frames[index]
        kind = frame.get("sessionUpdate")
        tool_id = frame.get("toolCallId")
        if kind == "tool_call_update" and isinstance(tool_id, str):
            owner = self._tool_owners.get(tool_id)
            return (replace(owner, frame=frame), (index,)) if owner is not None else None
        if kind == "tool_call" and isinstance(tool_id, str):
            item = self._take_tool(frame, tool_id, steps)
            return (item, (index,)) if item is not None else None
        field_name = "thinking" if kind == "agent_thought_chunk" else "content"
        return self._take_text(index, frames, consumed, steps, field_name, interleave=interleave)

    def _owner(
        self, step: PlannerRow, frame: Frame, *, text_finished: bool = False
    ) -> ProjectedFrame | None:
        owner = self._route(step)
        if isinstance(owner, str):
            return ProjectedFrame(frame, owner, None, text_finished=text_finished)
        if owner is not None:
            reply = content_text(frame.get("content")) if text_finished and step.final else ""
            return ProjectedFrame(
                frame, None, owner, reply, text_finished, step.index if reply else None
            )
        return None

    def _take_tool(
        self, frame: Frame, tool_id: str, steps: Sequence[PlannerRow]
    ) -> ProjectedFrame | None:
        matches = [
            (step, evidence)
            for step in steps
            for index, call in enumerate(_calls(step.row))
            if (evidence := (step.index, "tool", index)) not in self.seen
            and _matches_call(frame, call)
        ]
        if len(matches) != 1:
            return None
        step, evidence = matches[0]
        owner = self._owner(step, frame)
        if owner is not None:
            self.seen.add(evidence)
            self._tool_owners[tool_id] = owner
        return owner

    def _take_text(
        self,
        index: int,
        frames: Sequence[Frame],
        consumed: set[int],
        steps: Sequence[PlannerRow],
        name: str,
        *,
        interleave: bool,
    ) -> tuple[ProjectedFrame, tuple[int, ...]] | None:
        kind = frames[index].get("sessionUpdate")
        first = content_text(frames[index].get("content"))
        options = [
            (step, value.strip())
            for step in steps
            if (step.index, name, 0) not in self.seen
            and isinstance(value := step.row.get(name), str)
            and value.strip().startswith(first.strip())
        ]
        if not options:
            return None
        step, value = options[0]
        route = self._route(step)
        if route is None or any(self._route(other) != route for other, _ in options[1:]):
            return None
        indices = _text_chunks(index, first, value, frames, consumed, interleave=interleave)
        if indices:
            frame = {"sessionUpdate": kind, "content": {"type": "text", "text": value}}
            owner = self._owner(step, frame, text_finished=name == "content")
            if owner is not None:
                self.seen.add((step.index, name, 0))
                return owner, indices
        return None


def _text_chunks(
    index: int,
    first: str,
    expected: str,
    frames: Sequence[Frame],
    consumed: set[int],
    *,
    interleave: bool,
) -> tuple[int, ...]:
    kind = frames[index].get("sessionUpdate")
    text = first
    indices = [index]
    for current in range(index + 1, len(frames)):
        if text.strip() == expected:
            return tuple(indices)
        if current in consumed or frames[current].get("sessionUpdate") != kind:
            continue
        candidate = text + content_text(frames[current].get("content"))
        if expected.startswith(candidate.strip()):
            text = candidate
            indices.append(current)
        elif not interleave:
            break
    return tuple(indices) if text.strip() == expected else ()


def _calls(row: dict[str, object]) -> Iterator[dict[str, object]]:
    calls = row.get("tool_calls")
    if is_object_list(calls):
        yield from (call for call in calls if is_object_dict(call))


def _matches_call(frame: dict[str, object], call: dict[str, object]) -> bool:
    return (frame.get("name") or frame.get("title")) == call.get("name") and (
        frame.get("rawInput", frame.get("args")) or {}
    ) == (call.get("args") or {})


def _nonnegative_tokens(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def planner_step_usage(rows: Sequence[dict[str, object]]) -> tuple[Usage, ...]:
    if not all(has_planner_tokens(row) for row in rows):
        return ()
    return tuple(planner_tokens(row) for row in rows)


def has_planner_tokens(row: dict[str, object]) -> bool:
    return type(row.get("input_tokens")) is int and type(row.get("output_tokens")) is int


def planner_tokens(row: dict[str, object]) -> Usage:
    cached = _nonnegative_tokens(row.get("cache_read_tokens"))
    return Usage(
        _nonnegative_tokens(row.get("input_tokens")) + cached,
        _nonnegative_tokens(row.get("output_tokens")),
        cache_read_input_tokens=cached,
    )


@dataclass(frozen=True, slots=True)
class NativeReply:
    notice: BackgroundNotice
    start: int
    steps: Sequence[PlannerRow]

    def events(
        self, turn_id: str, session_id: str | None, generation: int
    ) -> tuple[TurnStarted, AssistantText, TurnCompleted]:
        text = content_text({"type": "text", "text": self.steps[-1].row["content"]}).strip()
        steps = planner_step_usage([step.row for step in self.steps])
        cause = (
            HarnessCause.BACKGROUND_TASK
            if self.notice.prompt.startswith('Task id "')
            else HarnessCause.SCHEDULED_WAKE
        )
        return (
            TurnStarted(
                turn_id,
                TurnOrigin.HARNESS,
                (),
                generation,
                cause=cause,
                detail=self.notice.sender,
                record_start=self.start,
            ),
            AssistantText(turn_id, text),
            TurnCompleted(
                turn_id=turn_id,
                session_id=session_id,
                text=text,
                subtype="success",
                is_error=False,
                interrupted=False,
                stop_reason="end_turn",
                num_turns=len(self.steps),
                duration_ms=0,
                usage=sum(steps, Usage()),
                cost_usd=None,
                cumulative_cost_usd=None,
                permission_denials=(),
                steps=steps,
                generation=generation,
                usage_uncertain=not steps,
            ),
        )


@dataclass(slots=True)
class IdleReplies:
    offset: int
    pending: bool = False
    _signature: tuple[int, ...] | None = None

    def read(self, session_id: str | None, *, home: Path | None) -> Iterator[NativeReply]:
        if not self.pending or session_id is None:
            return
        path = transcript_path(session_id, home=home)
        if path is None:
            return
        signature = RecordFile(path).watch_signature()
        if signature is None or signature == self._signature:
            return
        self._signature = signature
        yield from self._take(TranscriptSnapshot.read(path), session_id)

    def _take(self, transcript: TranscriptSnapshot, session_id: str) -> Iterator[NativeReply]:
        offset = self.offset
        pending: list[PlannerRow] = []
        for step in planner_rows(transcript.rows[offset:], ()):
            notice = step.notice
            if notice is None or not notice.is_task_for(session_id):
                continue
            if pending and pending[-1].notice != notice:
                pending.clear()
            if offset + notice.index < self.offset:
                continue
            pending.append(step)
            if step.final:
                self.offset = offset + step.index + 1
                yield NativeReply(notice, transcript.offsets[offset + notice.index], tuple(pending))
                pending.clear()
