from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb

from base.agents.messages.termination_notice import (
    AncestorTerminationNotice,
    ancestor_termination_payload,
    format_ancestor_termination_notice,
    should_notify_ancestor,
)
from base.db.protocols import AsyncConnectionLike
from base.db.rows import IdRow, query_one

__all__ = [
    "AncestorTerminationNotice",
    "notify_nearest_living_ancestor_async",
    "notify_nearest_living_ancestor_sync",
]

_MAX_ANCESTOR_DEPTH = 64

_NEAREST_LIVING_ANCESTOR_SQL = (
    "WITH RECURSIVE spawner_walk(agent_id, spawner, status, path, depth) AS ("
    "  SELECT id, COALESCE(born_spawner, spawner), status, ARRAY[id], 0 "
    "  FROM agents_meta WHERE id = %s "
    "  UNION ALL "
    "  SELECT parent.id, COALESCE(parent.born_spawner, parent.spawner), parent.status, "
    "         walk.path || parent.id, walk.depth + 1 "
    "  FROM spawner_walk walk "
    "  JOIN agents_meta parent "
    "    ON parent.id = substring(walk.spawner FROM '^agent:([0-9]+)$')::bigint "
    "  WHERE walk.depth < %s AND NOT parent.id = ANY(walk.path)"
    ") "
    "SELECT agent_id AS id FROM spawner_walk "
    "WHERE depth > 0 AND status <> 'terminated' "
    "ORDER BY depth ASC LIMIT 1"
)

_AGENT_LABEL_SQL = "SELECT label FROM agents WHERE id = %s"

_INSERT_NOTICE_SQL = (
    "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
    "VALUES (%s, %s, 'system_note', 'system', %s) RETURNING id"
)


def _extract_label(row: tuple[object, ...] | None) -> str | None:
    if row is None:
        return None
    raw = row[0]
    return raw if isinstance(raw, str) else None


def _extract_id(row: tuple[object, ...] | None) -> int | None:
    if row is None:
        return None
    raw = row[0]
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    raise TypeError(f"expected integer id column, got {raw!r}")


def notify_nearest_living_ancestor_sync(
    conn: psycopg.Connection,
    *,
    terminated_agent_id: int,
    source: str,
    message: str | None,
) -> AncestorTerminationNotice | None:
    ancestor = query_one(
        conn,
        IdRow,
        _NEAREST_LIVING_ANCESTOR_SQL,
        (terminated_agent_id, _MAX_ANCESTOR_DEPTH),
    )
    ancestor_id = None if ancestor is None else ancestor.id
    if not should_notify_ancestor(
        ancestor_id,
        source=source,
        terminated_agent_id=terminated_agent_id,
    ):
        return None
    assert ancestor_id is not None  # noqa: S101
    label_row = conn.execute(_AGENT_LABEL_SQL, (terminated_agent_id,)).fetchone()
    content = format_ancestor_termination_notice(
        terminated_agent_id=terminated_agent_id,
        label=_extract_label(label_row),
        source=source,
        message=message,
    )
    payload = Jsonb(
        ancestor_termination_payload(terminated_agent_id=terminated_agent_id, source=source)
    )
    inserted = query_one(conn, IdRow, _INSERT_NOTICE_SQL, (ancestor_id, content, payload))
    if inserted is None:
        raise RuntimeError("expected inbound id after ancestor termination notice INSERT")
    return AncestorTerminationNotice(ancestor_id=ancestor_id, inbound_id=inserted.id)


async def notify_nearest_living_ancestor_async(
    conn: AsyncConnectionLike,
    *,
    terminated_agent_id: int,
    source: str,
    message: str | None,
) -> AncestorTerminationNotice | None:
    ancestor_row = await (
        await conn.execute(
            _NEAREST_LIVING_ANCESTOR_SQL,
            (terminated_agent_id, _MAX_ANCESTOR_DEPTH),
        )
    ).fetchone()
    ancestor_id = _extract_id(ancestor_row)
    if not should_notify_ancestor(
        ancestor_id,
        source=source,
        terminated_agent_id=terminated_agent_id,
    ):
        return None
    assert ancestor_id is not None  # noqa: S101
    label_row = await (await conn.execute(_AGENT_LABEL_SQL, (terminated_agent_id,))).fetchone()
    content = format_ancestor_termination_notice(
        terminated_agent_id=terminated_agent_id,
        label=_extract_label(label_row),
        source=source,
        message=message,
    )
    payload = Jsonb(
        ancestor_termination_payload(terminated_agent_id=terminated_agent_id, source=source)
    )
    inserted_row = await (
        await conn.execute(_INSERT_NOTICE_SQL, (ancestor_id, content, payload))
    ).fetchone()
    inbound_id = _extract_id(inserted_row)
    if inbound_id is None:
        raise RuntimeError("expected inbound id after ancestor termination notice INSERT")
    return AncestorTerminationNotice(ancestor_id=ancestor_id, inbound_id=inbound_id)
