from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg import Connection

from base.agents.history.timeline_item import TimelineItem
from base.agents.messages.inbound import InboundKind
from base.agents.messages.inbound_images import inbound_upload_files
from base.agents.messages.kwargs import NoteTag
from base.host.boundary.json_values import is_object_dict, is_object_list

# meiva:note
# Which inbox rows were handed to a harness as one input, read back from the inbox, and what each of them shows on
# the timeline. An input is sent to the CLI with an id derived from its key, and the row of the CLI's record that holds
# it names only that id, so who said what is recovered here: the rows the agent claimed around the time the record's
# rows were written, grouped by the key the claim marked them with (a batch handed over as one input shares a key) or
# `inbound-<id>` for a row sent alone. A row shows what the person typed (a `/command` is not expanded and no
# envelope is added), its sender, its images and its files. A system note or a heartbeat shows as a marker; the notes
# only the host's own lifecycle wrote (a restart, an interrupt, a lost turn) are not shown, because the record says what
# happened. The time range is the record's own rows' stamps widened by `CLAIM_SLACK`, so a request reads rows in
# proportion to the window and not to the agent's whole history.
# meiva:link base/lm/harness/docs/transcript-projection.md

INPUT_KEY_FIELD = "harness_input_key"
CLAIM_SLACK = timedelta(minutes=10)
DELIVERABLE_KINDS = (
    InboundKind.CHAT.value,
    InboundKind.SYSTEM_NOTE.value,
    InboundKind.HEARTBEAT.value,
)
_HIDDEN_NOTES = frozenset(
    {
        NoteTag.LIFECYCLE_RESTART.value,
        NoteTag.LIFECYCLE_RESURRECT.value,
        NoteTag.LIFECYCLE_INTERRUPT.value,
        NoteTag.TURN_LOST.value,
    }
)
_BATCH_ID_LENGTH = 32


@dataclass(frozen=True, slots=True)
class SentRow:
    id: int
    agent_id: int
    kind: str
    source: str
    content: str
    payload: Mapping[str, object] | None
    created_at: datetime | None

    @property
    def input_key(self) -> str:
        key = self.payload.get(INPUT_KEY_FIELD) if self.payload else None
        return key if isinstance(key, str) else f"inbound-{self.id}"


def load_sent_rows(
    conn: Connection, agent_id: int, *, since: datetime, until: datetime
) -> list[SentRow]:
    cursor = conn.execute(
        "SELECT id, agent_id, kind, source, content, payload, created_at FROM inbound_messages "
        "WHERE agent_id=%s AND claimed_at >= %s AND claimed_at <= %s AND kind = ANY(%s) "
        "ORDER BY created_at ASC, id ASC",
        (agent_id, since - CLAIM_SLACK, until + CLAIM_SLACK, list(DELIVERABLE_KINDS)),
    )
    return [_row(values) for values in cursor.fetchall()]


def _row(values: tuple[object, ...]) -> SentRow:
    match values:
        case (
            int() as id_,
            int() as agent,
            str() as kind,
            str() as source,
            str() as content,
            payload,
            created,
        ):
            return SentRow(
                id=id_,
                agent_id=agent,
                kind=kind,
                source=source,
                content=content,
                payload=payload if is_object_dict(payload) else None,
                created_at=created if isinstance(created, datetime) else None,
            )
        case _:
            raise ValueError(f"an inbox row of an unexpected shape: {values!r}")


def sent_inputs(rows: Sequence[SentRow]) -> dict[str, list[TimelineItem]]:
    grouped: dict[str, list[TimelineItem]] = {}
    for row in rows:
        items = grouped.setdefault(row.input_key, [])
        item = row_item(row)
        if item is not None:
            items.append(item)
    return grouped


def row_item(row: SentRow) -> TimelineItem | None:
    created = None if row.created_at is None else row.created_at.isoformat()
    if row.kind == InboundKind.CHAT.value:
        images = _image_urls(row)
        return TimelineItem(
            item_id="pending",
            kind="inbound_chat",
            source=row.source,
            payload=row.content,
            created_at=created,
            inbound_id=row.id,
            images=images or None,
            files=inbound_upload_files(row.agent_id, row.payload),
        )
    tag = _note_tag(row)
    if tag in _HIDDEN_NOTES:
        return None
    return TimelineItem(
        item_id="pending",
        kind="system_marker",
        source=tag,
        payload=row.content,
        created_at=created,
    )


def _note_tag(row: SentRow) -> str:
    if row.kind == InboundKind.HEARTBEAT.value:
        return NoteTag.HEARTBEAT.value
    tag = row.payload.get("note_tag") if row.payload else None
    if not isinstance(tag, str):
        raise TypeError(f"system_note inbound {row.id} has no 'note_tag'")
    return tag


def _image_urls(row: SentRow) -> list[str]:
    payload = row.payload or {}
    urls: list[str] = []
    uploads = payload.get("image_uploads")
    for raw in uploads if is_object_list(uploads) else []:
        if not is_object_dict(raw):
            continue
        batch, ordinal = raw.get("batch_id"), raw.get("ordinal")
        if isinstance(batch, str) and len(batch) == _BATCH_ID_LENGTH and isinstance(ordinal, int):
            urls.append(f"/api/agents/{row.agent_id}/uploads/{batch}/{ordinal}")
    blocks = payload.get("content_blocks")
    for block in blocks if is_object_list(blocks) else []:
        if is_object_dict(block) and block.get("type") == "image_url":
            image = block.get("image_url")
            url = image.get("url") if is_object_dict(image) else None
            if isinstance(url, str):
                urls.append(url)
    return urls
