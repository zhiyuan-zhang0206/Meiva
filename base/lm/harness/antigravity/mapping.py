from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.jsonl import read_jsonl_rows
from base.host.env.harness_binaries import resolve_user_home
from base.lm.harness.antigravity.acp import (
    UnrecognisedError,
    classify_system_message,
    content_text,
    input_marker,
    received_markers,
)
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
    def for_session(cls, session_id: str | None, *, home: Path | None) -> TranscriptSnapshot:
        return cls.read(transcript_path(session_id, home=home) if session_id else None)

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
    input_index: int

    @property
    def carries_notice(self) -> bool:
        return self.notice is not None and self.notice.index < self.input_index

    @property
    def final(self) -> bool:
        text = self.row.get("content")
        return isinstance(text, str) and bool(text.strip()) and not self.row.get("tool_calls")

    @property
    def blank_reply(self) -> bool:
        text = self.row.get("content")
        return not self.row.get("tool_calls") and (
            text is None or (isinstance(text, str) and not text.strip())
        )

    @property
    def terminal(self) -> bool:
        tokens = self.row.get("output_tokens")
        return self.final or (self.blank_reply and type(tokens) is int and tokens == 0)


def _launched_command(row: Frame) -> str | None:
    content, step = row.get("content"), row.get("step_index")
    if row.get("source") != "MODEL" or row.get("status") != "RUNNING":
        return None
    if not isinstance(content, str) or type(step) is not int:
        return None
    header, _, body = content.partition("\n")
    line = body.partition("\n")[0]
    prefix = "Tool is running as a background task with task id: "
    task = line.removeprefix(prefix)
    if (
        header.startswith("Created At: ")
        and line.startswith(prefix)
        and task.endswith(f"/task-{step}")
        and len(task.split()) == 1
    ):
        return task
    return None


def planner_rows(
    rows: Sequence[Frame],
    keys: Sequence[str],
    *,
    boundaries: Mapping[str, ForegroundBoundary] | None = None,
    answered_at: Mapping[int, int] | None = None,
) -> Iterator[PlannerRow]:
    for event in _attribution_events(rows, keys, boundaries or {}, answered_at or {}):
        if isinstance(event, PlannerRow):
            yield event


def _attribution_events(
    rows: Sequence[Frame],
    keys: Sequence[str],
    boundaries: Mapping[str, ForegroundBoundary],
    answered_at: Mapping[int, int],
) -> Iterator[PlannerRow | BackgroundNotice | None]:
    marks = {input_marker(key): key for key in keys}
    foreground: str | None = None
    input_index = -1
    background: BackgroundNotice | None = None
    notice_boundary: ForegroundBoundary | None = None
    boundary: ForegroundBoundary | None = None
    commands: set[str] | None = None
    for index, row in enumerate(rows):
        kind = row.get("type")
        if kind == "USER_INPUT":
            input_index = index
            marker = next(iter(received_markers((row,))), "")
            foreground = marks.get(marker)
            boundary = boundaries.get(foreground or "")
            commands = set() if foreground is not None else None
            background = _carry_notice(background, notice_boundary, answered_at, index)
            yield background
        elif kind == "GENERIC":
            _remember_launched_command(row, commands)
        elif kind == "SYSTEM_MESSAGE" and (notice := _background_notice(row, index)) is not None:
            if not notice.completes_command(commands, boundary):
                background = notice
                notice_boundary = boundary
                yield notice
        elif kind == "PLANNER_RESPONSE" and row.get("status") == "DONE":
            step = PlannerRow(index, row, foreground, background, input_index)
            if step.terminal:
                commands = None
            yield step


def _carry_notice(
    notice: BackgroundNotice | None,
    boundary: ForegroundBoundary | None,
    answered_at: Mapping[int, int],
    marker_index: int,
) -> BackgroundNotice | None:
    if notice is None or boundary is None or not boundary.degraded:
        return None
    if answered_at.get(notice.index, marker_index) < marker_index:
        return None
    return notice


def _remember_launched_command(row: Frame, commands: set[str] | None) -> None:
    if commands is not None and (task := _launched_command(row)) is not None:
        commands.add(task)


class ChildLookup(Protocol):
    def is_child(self, conversation_id: str, child_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class BackgroundNotice:
    sender: str
    prompt: str
    index: int
    cause: HarnessCause

    def completes_command(
        self, commands: set[str] | None, boundary: ForegroundBoundary | None
    ) -> bool:
        return (
            commands is not None
            and self.sender in commands
            and (boundary is None or self.index < boundary.next_row)
            and self.prompt.startswith(f'Task id "{self.sender}" finished with result:')
        )

    def belongs_to(self, session_id: str | None, subagents: ChildLookup | None = None) -> bool:
        if session_id is None:
            return False
        return self.sender.startswith(session_id + "/task-") or (
            subagents is not None and subagents.is_child(session_id, self.sender)
        )


def _background_notice(row: dict[str, object], index: int) -> BackgroundNotice | None:
    content = row.get("content")
    if not isinstance(content, str):
        return None
    try:
        notice = classify_system_message(content)
    except UnrecognisedError:
        return None
    return BackgroundNotice(notice.sender, notice.text, index, notice.cause) if notice else None


@dataclass(frozen=True, slots=True)
class ProjectedFrame:
    frame: dict[str, object]
    input_key: str | None
    notice: BackgroundNotice | None
    reply: str = ""
    text_finished: bool = False

    def native_notice(
        self, session_id: str | None, subagents: ChildLookup | None = None
    ) -> BackgroundNotice | None:
        if self.reply and self.notice is not None and self.notice.belongs_to(session_id, subagents):
            return self.notice
        return None


Frame = dict[str, object]
Evidence = tuple[int, str, int]


@dataclass(frozen=True, slots=True)
class ForegroundBoundary:
    next_row: int
    accounted: bool
    cancelled: bool
    degraded: bool

    def excludes(self, step: PlannerRow, notice: BackgroundNotice) -> bool:
        tokens = step.row.get("input_tokens")
        if notice.index >= self.next_row:
            return True
        if step.index < self.next_row:
            return False
        if self.degraded:
            return True
        if self.cancelled:
            return step.final
        return self.accounted and type(tokens) is int and tokens > 0


@dataclass(slots=True)
class OutputProjection:
    offset: int
    fences_replay: bool = False
    keys: list[str] = field(default_factory=list[str])
    pending: deque[Frame] = field(default_factory=deque[Frame])
    confirmed: set[int] = field(default_factory=set[int])
    excluded: set[int] = field(default_factory=set[int])
    _answered_at: dict[int, int] = field(default_factory=dict[int, int])
    _boundaries: dict[str, ForegroundBoundary] = field(
        default_factory=dict[str, ForegroundBoundary]
    )
    seen: set[Evidence] = field(default_factory=set[Evidence])
    _tool_owners: dict[str, ProjectedFrame] = field(default_factory=dict[str, ProjectedFrame])

    def owns(self, step: PlannerRow) -> bool:
        return step.input_key is not None and (step.notice is None or step.index in self.confirmed)

    def steps(self, rows: Sequence[Frame]) -> tuple[PlannerRow, ...]:
        return tuple(
            planner_rows(
                rows[self.offset :],
                self.keys,
                boundaries=self._boundaries,
                answered_at=self._answered_at,
            )
        )

    def confirm_span(self, span: Sequence[PlannerRow], candidates: Sequence[PlannerRow]) -> None:
        selected = {step.index for step in span}
        self.confirmed.update(selected)
        self.excluded.update(step.index for step in candidates if step.index not in selected)

    def close_foreground(
        self,
        keys: Sequence[str],
        row_count: int,
        *,
        accounted: bool,
        cancelled: bool = False,
        degraded: bool = False,
    ) -> None:
        boundary = ForegroundBoundary(row_count - self.offset, accounted, cancelled, degraded)
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

    def background_replies(
        self, transcript: TranscriptSnapshot, session_id: str | None, subagents: ChildLookup
    ) -> Iterator[NativeReply]:
        yield from native_replies(
            transcript,
            self.offset,
            self.steps(transcript.rows),
            session_id,
            subagents,
            self._answered_at,
            route=self._route,
        )

    def has_pending_reply(
        self, rows: Sequence[Frame], session_id: str | None, subagents: ChildLookup
    ) -> bool:
        notice = None
        for event in _attribution_events(
            rows[self.offset :], self.keys, self._boundaries, self._answered_at
        ):
            if event is None or isinstance(event, BackgroundNotice):
                notice = event
        return (
            notice is not None
            and notice.index not in self._answered_at
            and notice.belongs_to(session_id, subagents)
        )

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
            return ProjectedFrame(frame, None, owner, reply, text_finished)
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


def corroborates_assistant_chunks(
    frames: Sequence[Frame], foreground: Sequence[str], detached: Sequence[str]
) -> bool:
    text_frames = {
        index for index, frame in enumerate(frames) if content_text(frame.get("content")).strip()
    }
    if not foreground or not text_frames:
        return False
    consumed: set[int] = set()
    cursor = 0
    for text in foreground:
        indices = _find_text_chunks(frames, text, consumed, start=cursor)
        if not indices:
            return False
        consumed.update(indices)
        cursor = indices[-1] + 1
    for text in detached:
        consumed.update(_find_text_chunks(frames, text, consumed))
    return text_frames <= consumed


def _find_text_chunks(
    frames: Sequence[Frame],
    expected: str,
    consumed: set[int],
    *,
    start: int = 0,
) -> tuple[int, ...]:
    for interleave in (False, True):
        for index in range(start, len(frames)):
            first = content_text(frames[index].get("content"))
            if index in consumed or not expected.startswith(first.strip()):
                continue
            indices = _text_chunks(index, first, expected, frames, consumed, interleave=interleave)
            if indices:
                return indices
    return ()


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
    transcript: TranscriptSnapshot
    offset: int
    steps: Sequence[PlannerRow]
    incomplete_span: bool = False

    @property
    def timer_prompt(self) -> str | None:
        return self.notice.prompt if self.notice.cause is HarnessCause.SCHEDULED_WAKE else None

    def events(
        self, turn_id: str, session_id: str | None, generation: int
    ) -> tuple[TurnStarted, AssistantText, TurnCompleted]:
        text = content_text({"type": "text", "text": self.steps[-1].row["content"]}).strip()
        steps = tuple(planner_tokens(step.row) for step in self.steps)
        complete_usage = all(has_planner_tokens(step.row) for step in self.steps)
        return (
            TurnStarted(
                turn_id,
                TurnOrigin.HARNESS,
                (),
                generation,
                cause=self.notice.cause,
                detail=self.notice.sender,
                record_start=self.transcript.offsets[self.offset + self.notice.index],
                record_reply=self.transcript.offsets[self.offset + self.steps[-1].index],
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
                usage_uncertain=self.incomplete_span or not complete_usage,
            ),
        )


@dataclass(slots=True)
class IdleReplies:
    offset: int
    pending: bool = False
    _signature: tuple[int, ...] | None = None

    def bind(self, session_id: str, *, home: Path | None) -> None:
        if not self.offset:
            self.offset = len(TranscriptSnapshot.for_session(session_id, home=home).rows)

    def changed(
        self, session_id: str | None, *, home: Path | None, projecting: bool
    ) -> TranscriptSnapshot | None:
        if not (self.pending or projecting) or session_id is None:
            return None
        path = transcript_path(session_id, home=home)
        if path is None:
            return None
        signature = RecordFile(path).watch_signature()
        if signature is None or signature == self._signature:
            return None
        self._signature = signature
        return TranscriptSnapshot.read(path)

    def replies(
        self, transcript: TranscriptSnapshot, session_id: str | None, subagents: ChildLookup
    ) -> Iterator[NativeReply]:
        for reply in native_replies(
            transcript,
            self.offset,
            planner_rows(transcript.rows[self.offset :], ()),
            session_id,
            subagents,
            {},
        ):
            self.offset = reply.offset + reply.steps[-1].index + 1
            yield reply


def native_replies(
    transcript: TranscriptSnapshot,
    offset: int,
    steps: Iterable[PlannerRow],
    session_id: str | None,
    subagents: ChildLookup,
    answered_at: dict[int, int],
    *,
    route: Callable[[PlannerRow], str | BackgroundNotice | None] | None = None,
) -> Iterator[NativeReply]:
    pending: dict[BackgroundNotice, list[PlannerRow]] = {}
    unresolved: set[int] = set()
    for step in steps:
        notice = step.notice
        if (
            notice is None
            or notice.index in answered_at
            or not notice.belongs_to(session_id, subagents)
        ):
            continue
        if route is not None:
            owner = route(step)
            if owner is None:
                unresolved.add(notice.index)
            if owner != notice:
                continue
        span = pending.setdefault(notice, [])
        span.append(step)
        if step.final:
            answered_at[notice.index] = step.index
            yield NativeReply(
                notice, transcript, offset, tuple(span), incomplete_span=notice.index in unresolved
            )
            del pending[notice]
