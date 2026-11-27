from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from psycopg import Connection

from base.agents.history.timeline_item import TimelineItem
from base.agents.messages.harness_input_key import (
    handed_input_keys,
)
from base.agents.messages.image_dimensions import ImageDimensions
from base.agents.messages.inbound import InboundKind
from base.agents.messages.inbound_images import (
    inbound_image_dimensions,
    inbound_upload_files,
)
from base.agents.messages.kwargs import NoteTag
from base.agents.messages.quote import payload_quote
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.lm.harness.session.transcript.entries import InputRow
from base.log import logger

# meiva:note
# Which inbox rows were handed to a harness as one input, read back from the inbox, and what each of them shows on
# the timeline. An input is sent to the CLI with an id derived from its key, and the row of the CLI's record that holds
# it names only that id, so who said what is recovered here: the agent's inbox rows, grouped by the key the claim marked them with (a batch handed over as one input shares a key) or
# the row's own key for a row sent alone (`base/agents/messages/harness_input_key.py`). A row shows what the person typed (a `/command` is not expanded and no
# envelope is added), its sender, its images and its files. A system note or a heartbeat shows as a marker; the notes
# only the host's own lifecycle wrote (a restart, an interrupt, a lost turn) are not shown, because the record says what
# happened. How the rows are found is `load_sent_rows`.
# meiva:link base/lm/harness/docs/transcript-projection.md

CLAIM_EARLY = timedelta(seconds=5)
CLAIM_SLACK = timedelta(minutes=10)
BATCH_ROWS = 200
MAX_ROWS = 1000
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
    claimed_at: datetime | None = None

    # meiva:note
    # The keys the record may hold this row under: a row a halted turn handed back was handed over
    # again under a new key, and the record keeps the earlier handover too.
    @property
    def input_keys(self) -> tuple[str, ...]:
        return handed_input_keys(self.id, self.payload)


# meiva:note
# The inbox rows that account for the person's inputs in a stretch of the record, found by the one fact that ties them: the
# row's input key gives the id the CLI kept (`wire_id`), so a row of the record is matched to its inbox row by id and by no
# window of time. `wanted` maps the ids to match to the time of their rows in the record. The agent's deliverable rows are
# read newest first, `BATCH_ROWS` at a time, from the last created at or before `until` (an index scan on
# `(agent_id, created_at)` with a limit, so what a request reads follows `BATCH_ROWS` and `MAX_ROWS` and never the agent's
# whole inbox), and reading goes on while an id is unmatched. An id is given up on, with no further reading for it, when the rows
# read have reached ones claimed more than `CLAIM_SLACK` before its row was written: rows are claimed in the order they were
# created, so an older row cannot be the one, and the id is the host's own input (a context reset, a recovery note) that no
# inbox row names; that costs one batch at most. When `MAX_ROWS` are read, an id whose row was written before the oldest
# row read could claim it (more than `CLAIM_EARLY` earlier than that claim) is beyond this read's reach, the scan having
# stopped at the cap: it is not shown and not reported, for it is the range that ended and not an input that no row names.
# An id still unmatched within the range read is reported (`harness_timeline_inputs_unmatched`, with the count) and not
# shown.
@dataclass(frozen=True, slots=True)
class Found:
    rows: list[SentRow]
    scanned: int


def load_sent_rows(
    conn: Connection,
    agent_id: int,
    *,
    until: datetime,
    wanted: Mapping[str, InputRow],
    wire_id: Callable[[str], str],
) -> Found:
    pending = dict(wanted)
    found: list[SentRow] = []
    scanned = 0
    while pending and scanned < MAX_ROWS:
        batch = _batch(conn, agent_id, until + CLAIM_SLACK, scanned)
        scanned += len(batch)
        found.extend(row for row, _claimed in batch)
        _settle(pending, batch, wire_id)
        if len(batch) < BATCH_ROWS:
            break
    if scanned >= MAX_ROWS:
        _forget_beyond_reach(pending, found)
    if pending and scanned >= MAX_ROWS:
        logger.warning(
            "{count} inputs in the record match no inbox row within the newest {scanned} of the agent's rows; "
            "they are not shown",
            event="harness_timeline_inputs_unmatched",
            agent_id=agent_id,
            count=len(pending),
            scanned=scanned,
        )
    mine = _matched_rows(found, wanted, wire_id)
    ordered = sorted(
        mine, key=lambda row: (row.created_at or datetime.min.replace(tzinfo=UTC), row.id)
    )
    return Found(ordered, scanned)


def quoted_inbound_matches(
    conn: Connection,
    agent_id: int,
    inbound_id: int,
    *,
    wanted: frozenset[str],
    wire_id: Callable[[str], str],
) -> bool:
    row = conn.execute(
        "SELECT id, agent_id, kind, source, content, payload, created_at FROM inbound_messages "
        "WHERE agent_id=%s AND id=%s AND kind=%s AND claimed_at IS NOT NULL",
        (agent_id, inbound_id, InboundKind.CHAT.value),
    ).fetchone()
    return row is not None and any(wire_id(key) in wanted for key in _row(row).input_keys)


def _matched_rows(
    found: Sequence[SentRow], wanted: Mapping[str, InputRow], wire_id: Callable[[str], str]
) -> list[SentRow]:
    return [row for row in found if any(wire_id(key) in wanted for key in row.input_keys)]


# meiva:note
# What one batch settles of the ids still wanted: the ones its rows carry are matched, and the ones whose rows were written
# long after the oldest claim in the batch are given up on (see `load_sent_rows`).
def _settle(
    pending: dict[str, InputRow],
    batch: Sequence[tuple[SentRow, datetime | None]],
    wire_id: Callable[[str], str],
) -> None:
    for key in _matched_keys([row for row, _claimed in batch], wire_id) & pending.keys():
        del pending[key]
    oldest_claim = min((c for _row, c in batch if c is not None), default=None)
    if oldest_claim is None:
        return
    for wire, stamp in list(pending.items()):
        if oldest_claim < stamp.stamp - CLAIM_SLACK:
            del pending[wire]


def _matched_keys(rows: Sequence[SentRow], wire_id: Callable[[str], str]) -> set[str]:
    return {wire_id(key) for row in rows for key in row.input_keys}


# meiva:note
# The ids that were written before the oldest row read could claim them are dropped from what is wanted: they are not
# in this read's range (see `load_sent_rows`).
def _forget_beyond_reach(pending: dict[str, InputRow], found: Sequence[SentRow]) -> None:
    oldest = min((row.claimed_at for row in found if row.claimed_at is not None), default=None)
    if oldest is None:
        return
    for key, held in list(pending.items()):
        if held.stamp < oldest - CLAIM_EARLY:
            del pending[key]


def _batch(
    conn: Connection, agent_id: int, until: datetime, skip: int
) -> list[tuple[SentRow, datetime | None]]:
    cursor = conn.execute(
        "SELECT id, agent_id, kind, source, content, payload, created_at, claimed_at FROM inbound_messages "
        "WHERE agent_id=%s AND created_at <= %s AND kind = ANY(%s) AND claimed_at IS NOT NULL "
        "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
        (agent_id, until, list(DELIVERABLE_KINDS), BATCH_ROWS, skip),
    )
    return [
        (
            replace(
                _row(values[:7]), claimed_at=values[7] if isinstance(values[7], datetime) else None
            ),
            values[7] if isinstance(values[7], datetime) else None,
        )
        for values in cursor.fetchall()
    ]


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
        item = row_item(row)
        for key in row.input_keys:
            items = grouped.setdefault(key, [])
            if item is not None:
                items.append(item)
    return grouped


def row_item(row: SentRow) -> TimelineItem | None:
    created = None if row.created_at is None else row.created_at.isoformat()
    if row.kind == InboundKind.CHAT.value:
        images = _image_urls(row)
        image_dimensions = _image_dimensions(row, images) if images else None
        return TimelineItem(
            item_id="pending",
            kind="inbound_chat",
            source=row.source,
            payload=row.content,
            quote=payload_quote(row.payload),
            created_at=created,
            inbound_id=row.id,
            images=images or None,
            image_dimensions=image_dimensions,
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


def _pos_int(val: object) -> int | None:
    return val if isinstance(val, int) and not isinstance(val, bool) and val > 0 else None


def _upload_entry_dimensions(raw: object) -> tuple[bool, ImageDimensions | None]:
    if not is_object_dict(raw):
        return False, None
    batch, ordinal = raw.get("batch_id"), raw.get("ordinal")
    if not (isinstance(batch, str) and len(batch) == _BATCH_ID_LENGTH and isinstance(ordinal, int)):
        return False, None
    w, h = _pos_int(raw.get("width")), _pos_int(raw.get("height"))
    return True, ImageDimensions(width=w, height=h) if w is not None and h is not None else None


def _block_has_image_url(block: object) -> bool:
    if not is_object_dict(block) or block.get("type") != "image_url":
        return False
    image = block.get("image_url")
    return is_object_dict(image) and isinstance(image.get("url"), str)


def _image_dimensions(row: SentRow, images: Sequence[str]) -> list[ImageDimensions | None] | None:
    direct = inbound_image_dimensions(row.agent_id, row.payload)
    if direct is not None and len(direct) == len(images):
        return direct
    payload = row.payload or {}
    uploads = payload.get("image_uploads")
    blocks = payload.get("content_blocks")
    dims: list[ImageDimensions | None] = [
        entry_dims
        for raw in (uploads if is_object_list(uploads) else [])
        for valid, entry_dims in (_upload_entry_dimensions(raw),)
        if valid
    ]
    if is_object_list(blocks):
        dims.extend(None for block in blocks if _block_has_image_url(block))
    if len(dims) != len(images) or not any(entry is not None for entry in dims):
        return None
    return dims


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
