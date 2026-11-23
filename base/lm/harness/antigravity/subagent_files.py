from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from base.host.boundary.untyped import object_of
from base.host.env.harness_binaries import resolve_user_home
from base.lm.harness.antigravity.mapping import (
    IdleReplies,
    NativeReply,
    OutputProjection,
    TranscriptSnapshot,
    transcript_rows,
)
from base.lm.harness.antigravity.subagents import (
    Child,
    ChildCursor,
    advance,
    find_spawn,
    parse_child,
    spawn_events,
)
from base.lm.harness.events import BridgeEvent
from base.log import logger


# meiva:note
# The effect half of Antigravity sub-agent reading: what a conversation's children are and what each has
# written since the last poll. A poll reads the parent's `subagents/` descriptors and, for each child, its
# transcript, and returns the events the new rows make (see `subagents.py` for the file shapes). It is
# display only, so a descriptor or transcript that cannot be read is logged once and left out: it never
# fails a turn. agy appends a child's rows to its transcript while the child runs (observed on 1.5.5), so
# a poll shows them within one poll interval; the final poll at the end of a turn shows the rest.
# meiva:link base/lm/harness/docs/subagent-text.md
@dataclass(slots=True)
class SubagentReader:
    home: Path | None
    cursors: dict[str, ChildCursor] = field(default_factory=dict)
    _warned: set[str] = field(default_factory=set)
    _deferred: dict[int, NativeReply] = field(default_factory=dict[int, NativeReply])

    def defer_replies(self, projection: OutputProjection | None, session_id: str | None) -> None:
        if projection is not None and session_id is not None:
            transcript = TranscriptSnapshot.for_session(session_id, home=self.home)
            for reply in projection.background_replies(transcript, session_id, None):
                self._deferred[reply.record_start] = reply

    def _registered_replies(self, session_id: str | None) -> tuple[NativeReply, ...]:
        if session_id is None or not self._deferred:
            return ()
        children = {child.conversation_id for child in self._children(session_id)}
        return tuple(
            self._deferred.pop(index)
            for index, reply in tuple(self._deferred.items())
            if reply.notice.sender in children
        )

    def replay_idle(
        self, idle: IdleReplies, projection: OutputProjection | None, session_id: str | None
    ) -> tuple[OutputProjection | None, tuple[NativeReply, ...], bool] | None:
        current = idle.replay(projection, session_id, self, home=self.home)
        deferred = self._registered_replies(session_id)
        if current is not None:
            projection, replies, needed = current
            return projection, (*deferred, *replies), needed
        return (projection, deferred, projection is not None) if deferred else None

    def adopt(self, conversation_id: str | None) -> None:
        if conversation_id is None:
            return
        # meiva:note
        # A resumed conversation's existing children belong to turns this session never saw: they are
        # marked shown up to what is on disk now, so only what they write from here on is reported.
        for child in self._children(conversation_id):
            rows = transcript_rows(child.conversation_id, home=self.home)
            self.cursors[child.conversation_id] = ChildCursor(seen=len(rows), announced=True)

    def poll(self, conversation_id: str, turn_id: str) -> tuple[BridgeEvent, ...]:
        events: list[BridgeEvent] = []
        for child in self._children(conversation_id):
            cursor = self.cursors.get(child.conversation_id, ChildCursor())
            if not cursor.announced:
                announced = self._announce(conversation_id, child, turn_id)
                if announced is None:
                    continue
                events.extend(announced)
                cursor = replace(cursor, announced=True)
            rows = transcript_rows(child.conversation_id, home=self.home)
            fresh, cursor = advance(rows, child, cursor, turn_id=turn_id)
            self.cursors[child.conversation_id] = cursor
            events.extend(fresh)
        return tuple(events)

    def _announce(
        self, conversation_id: str, child: Child, turn_id: str
    ) -> tuple[BridgeEvent, ...] | None:
        spawn = find_spawn(transcript_rows(conversation_id, home=self.home), child)
        if spawn is None:
            return None
        arguments, output = spawn
        return spawn_events(child, turn_id=turn_id, arguments=arguments, output=output)

    def has_children(self, conversation_id: str | None) -> bool:
        return conversation_id is not None and bool(self._children(conversation_id))

    def is_child(self, conversation_id: str, child_id: str) -> bool:
        return any(child.conversation_id == child_id for child in self._children(conversation_id))

    def _children(self, conversation_id: str) -> list[Child]:
        directory = (
            resolve_user_home(self.home)
            / ".gemini"
            / "antigravity-cli"
            / "brain"
            / conversation_id
            / ".system_generated"
            / "subagents"
        )
        if not directory.is_dir():
            return []
        children: list[Child] = []
        for path in sorted(directory.glob("*.json")):
            child = self._read(path)
            if child is not None:
                children.append(child)
        return children

    def _read(self, path: Path) -> Child | None:
        try:
            child = parse_child(object_of(json.loads, path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            child = None
        if child is None and str(path) not in self._warned:
            self._warned.add(str(path))
            logger.warning(
                "[{label}] {body}",
                label="harness",
                event="agy_subagent_descriptor_unreadable",
                body=f"an Antigravity sub-agent descriptor could not be read: {path.name}",
            )
        return child
