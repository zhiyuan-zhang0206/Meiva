from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from base.agents.history.timeline_item import TimelineItem
from base.agents.history.transcript_timeline.items import ReplyPolicy, format_tool_use
from base.agents.history.transcript_timeline.read import Window, read_window
from base.lm.harness.claude.delivery import key_uuid
from base.lm.harness.claude.transcript.window import RecordFile

FIXTURES = Path(__file__).parents[4] / "lm/harness/claude/transcript/tests/fixtures"
POLICY = ReplyPolicy(explicit_visibility=False, delivery_mode=None)
_UUID = re.compile(r"\b([0-9a-f]{8})(-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b")
_TOOL = re.compile(r"toolu_\w+")


def inbound(row_id: int, text: str = "hello") -> TimelineItem:
    return TimelineItem(
        item_id="pending",
        kind="inbound_chat",
        source="user",
        payload=text,
        inbound_id=row_id,
        created_at="2026-11-12T08:56:00+00:00",
    )


def fixture_text(scenario: str) -> str:
    return (FIXTURES / scenario / "main.jsonl").read_text()


# meiva:note
# The rows of a fixture with the id of each input we sent replaced by the id of an input key.
def with_wire_ids(text: str, wire_ids: Mapping[str, str]) -> str:
    for old, key in wire_ids.items():
        text = text.replace(old, key_uuid(key))
    return text


# meiva:note
# One more copy of a fixture's rows, every row and call named afresh.
def generation(text: str, index: int) -> str:
    renamed = _UUID.sub(lambda m: f"{index:08x}{m.group(2)}", text)
    return _TOOL.sub(lambda m: f"{m.group(0)}x{index}", renamed)


def write(path: Path, text: str) -> RecordFile:
    path.write_text(text)
    return RecordFile(path)


def window(
    file: RecordFile,
    *,
    limit: int = 1000,
    before: int | None = None,
    sent: Mapping[str, Sequence[TimelineItem]] | None = None,
) -> Window:
    return read_window(
        file,
        limit=limit,
        before=before,
        policy=POLICY,
        render_tool=format_tool_use,
        load_sent=lambda _until, _wanted: dict(sent or {}),
    )


def rows_of(text: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in text.splitlines()]
