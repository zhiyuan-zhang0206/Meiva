from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from base.host.boundary.untyped import object_of
from base.host.env.harness_binaries import resolve_user_home
from base.lm.harness.antigravity.mapping import (
    transcript_rows,
    transcript_tool_args,
    transcript_tool_output,
)
from base.lm.harness.antigravity.subagents import (
    Child,
    ChildCursor,
    advance,
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
# fails a turn. How soon a child's rows are visible is up to agy's own flushing of that transcript, which
# is not observed; a poll sees whatever is on disk, and a final poll at the end of a turn shows the rest.
# meiva:link base/lm/harness/docs/subagent-text.md
@dataclass(slots=True)
class SubagentReader:
    home: Path | None
    cursors: dict[str, ChildCursor] = field(default_factory=dict)
    _warned: set[str] = field(default_factory=set)

    def adopt(self, conversation_id: str) -> None:
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
        output = transcript_tool_output(conversation_id, child.spawn_step, home=self.home)
        if not output:
            return None
        arguments = transcript_tool_args(
            conversation_id, child.spawn_step, "invoke_subagent", home=self.home
        )
        return spawn_events(child, turn_id=turn_id, arguments=arguments, output=output)

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
