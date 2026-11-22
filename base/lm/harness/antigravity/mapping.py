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
class PlannerRow:
    row: dict[str, object]
    input_key: str | None
    notice: BackgroundNotice | None

    @property
    def final(self) -> bool:
        return bool(self.row.get("content")) and not self.row.get("tool_calls")


def planner_rows(rows: Sequence[dict[str, object]], keys: Sequence[str]) -> Iterator[PlannerRow]:
    marks = {input_marker(key): key for key in keys}
    foreground: str | None = None
    background: BackgroundNotice | None = None
    for row in rows:
        kind = row.get("type")
        if kind == "USER_INPUT":
            foreground = next(
                (marks[mark] for mark in received_markers((row,)) if mark in marks), None
            )
            background = None
        elif kind == "SYSTEM_MESSAGE" and (notice := _background_notice(row)) is not None:
            background = notice
        elif kind == "PLANNER_RESPONSE" and row.get("status") == "DONE":
            step = PlannerRow(row, foreground if background is None else None, background)
            yield step
            if step.final:
                background = None


_SENDER = re.compile(r"^\[Message\] (?:timestamp=\S+ )?sender=(\S+)([^\n]*)", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class BackgroundNotice:
    sender: str
    prompt: str

    def is_task_for(self, session_id: str | None) -> bool:
        return bool(session_id and self.sender.startswith(session_id + "/task-"))


def _background_notice(row: dict[str, object]) -> BackgroundNotice | None:
    content = row.get("content")
    match = _SENDER.search(content) if isinstance(content, str) else None
    if match is None or match[1] == "system":
        return None
    _, separator, prompt = match[2].partition(" content=")
    if separator and isinstance(content, str):
        prompt += content[match.end() :].partition("</SYSTEM_MESSAGE>")[0]
    return BackgroundNotice(match[1], prompt.strip())


@dataclass(frozen=True, slots=True)
class ProjectedFrame:
    frame: dict[str, object]
    input_key: str | None
    notice: BackgroundNotice | None
    reply: str = ""
    text_finished: bool = False

    def timer_notice(self, session_id: str | None) -> BackgroundNotice | None:
        if self.reply and self.notice is not None and self.notice.is_task_for(session_id):
            return self.notice
        return None


@dataclass(slots=True)
class OutputProjection:
    offset: int
    keys: list[str] = field(default_factory=list[str])
    pending: deque[dict[str, object]] = field(default_factory=deque[dict[str, object]])
    _cursor: dict[str, int] = field(default_factory=dict[str, int])
    _prefix: dict[str, str] = field(default_factory=dict[str, str])
    _tool_owners: dict[str, ProjectedFrame] = field(default_factory=dict[str, ProjectedFrame])

    def expects_tool(self, tool_id: object) -> bool:
        return isinstance(tool_id, str) and (
            tool_id in self._tool_owners
            or any(item.get("toolCallId") == tool_id for item in self.pending)
        )

    def project(self, rows: Sequence[dict[str, object]]) -> Iterator[ProjectedFrame]:
        steps = tuple(planner_rows(rows[self.offset :], self.keys))
        waiting: deque[dict[str, object]] = deque()
        blocked: set[str] = set()
        while self.pending:
            frame = self.pending.popleft()
            kind = str(frame.get("sessionUpdate"))
            projected = None if kind in blocked else self._take(frame, steps)
            if projected is None:
                waiting.append(frame)
                blocked.add(kind)
            else:
                yield projected
        self.pending = waiting

    def _take(self, frame: dict[str, object], steps: Sequence[PlannerRow]) -> ProjectedFrame | None:
        kind = frame.get("sessionUpdate")
        tool_id = frame.get("toolCallId")
        if kind == "tool_call_update" and isinstance(tool_id, str):
            owner = self._tool_owners.get(tool_id)
            return replace(owner, frame=frame) if owner is not None else None
        if kind == "tool_call" and isinstance(tool_id, str):
            calls = ((step, call) for step in steps for call in _calls(step.row))
            for index, (step, call) in enumerate(calls):
                if index == self._cursor.get("tool", 0) and _matches_call(frame, call):
                    self._cursor["tool"] = index + 1
                    projected = ProjectedFrame(frame, step.input_key, step.notice)
                    self._tool_owners[tool_id] = projected
                    return projected
            return None
        field_name = "thinking" if kind == "agent_thought_chunk" else "content"
        return self._take_text(frame, steps, field_name)

    def _take_text(
        self, frame: dict[str, object], steps: Sequence[PlannerRow], name: str
    ) -> ProjectedFrame | None:
        texts = (
            (step, value)
            for step in steps
            if isinstance(value := step.row.get(name), str) and value.strip()
        )
        for index, (step, value) in enumerate(texts):
            if index < self._cursor.get(name, 0):
                continue
            prefix = self._prefix.get(name, "") + content_text(frame.get("content"))
            received, expected = prefix.strip(), value.strip()
            if not expected.startswith(received):
                return None
            reply = ""
            if received == expected:
                self._cursor[name] = index + 1
                self._prefix.pop(name, None)
                if name == "content" and step.notice and step.final:
                    reply = expected
            else:
                self._prefix[name] = prefix
            return ProjectedFrame(
                frame,
                step.input_key,
                step.notice,
                reply,
                text_finished=name == "content" and received == expected,
            )
        return None


def _calls(row: dict[str, object]) -> Iterator[dict[str, object]]:
    calls = row.get("tool_calls")
    if is_object_list(calls):
        yield from (call for call in calls if is_object_dict(call))


def _matches_call(frame: dict[str, object], call: dict[str, object]) -> bool:
    return (frame.get("name") or frame.get("title")) == call.get("name") and (
        frame.get("rawInput", frame.get("args")) or {}
    ) == (call.get("args") or {})
