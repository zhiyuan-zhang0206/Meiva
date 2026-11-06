from datetime import datetime

from psycopg import Connection, sql
from psycopg.rows import dict_row, scalar_row
from psycopg.types.json import Jsonb

from base.api_contracts.user_delivery import (
    PublishMessage,
    ReplyExpectation,
    UserDeliveryMode,
    UserMessage,
)

COLUMNS = "id, agent_id, mode, source_key, content, response, suggestions, created_at, read_at, resolved_at, resolution, reply"


class DeliveryConflictError(ValueError):
    pass


def delivery_mode(conn: Connection, agent_id: int, default: UserDeliveryMode) -> UserDeliveryMode:
    row = conn.execute(
        "SELECT COALESCE(config_overlay ->> 'user_delivery_mode', "
        "birth_config ->> 'user_delivery_mode', %s) FROM agents_meta WHERE id = %s",
        (default.value, agent_id),
    ).fetchone()
    if row is None:
        raise LookupError(f"agent {agent_id} does not exist")
    return UserDeliveryMode(row[0])


def publish(
    conn: Connection,
    agent_id: int,
    source_key: str,
    body: PublishMessage,
    *,
    mode: UserDeliveryMode,
    created_at: datetime,
) -> UserMessage:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "INSERT INTO agent_user_messages (agent_id, source_key, mode, content, response, "
            "suggestions, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (agent_id, source_key) DO NOTHING",
            (
                agent_id,
                source_key,
                mode.value,
                body.content,
                body.response.value,
                Jsonb(body.suggestions),
                created_at,
            ),
        )
        cur.execute(
            sql.SQL(
                "SELECT {} FROM agent_user_messages WHERE agent_id=%s AND source_key=%s"
            ).format(sql.SQL(COLUMNS)),
            (agent_id, source_key),
        )
        message = UserMessage.model_validate(cur.fetchone())
    if (message.content, message.response, message.suggestions, message.mode) != (
        body.content,
        body.response,
        body.suggestions,
        mode,
    ):
        raise DeliveryConflictError("this publication key already identifies different content")
    return message


def messages(
    conn: Connection,
    agent_id: int,
    *,
    pending_only: bool = False,
    after_id: int = 0,
    limit: int | None = None,
) -> list[UserMessage]:
    pending = (
        " AND (read_at IS NULL OR (response='required' AND resolved_at IS NULL))"
        if pending_only
        else ""
    )
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            sql.SQL(
                "SELECT {} FROM agent_user_messages WHERE agent_id=%s AND id>%s AND content IS NOT NULL{} ORDER BY id LIMIT %s"
            ).format(sql.SQL(COLUMNS), sql.SQL(pending)),
            (agent_id, after_id, limit),
        )
        return [UserMessage.model_validate(row) for row in cur.fetchall()]


def latest_decision(conn: Connection, agent_id: int) -> int:
    with conn.cursor(row_factory=scalar_row) as cur:
        cur.execute(
            "SELECT COALESCE(MAX(id), 0) FROM agent_user_messages WHERE agent_id=%s", (agent_id,)
        )
        value: object = cur.fetchone()
    if not isinstance(value, int):
        raise TypeError("delivery aggregate must be an integer")
    return value


def pending_context(records: list[UserMessage]) -> str:
    if not records:
        return ""
    entries = [
        f"User delivery #{record.id} ({'awaiting reply' if record.response is ReplyExpectation.REQUIRED and record.resolved_at is None else 'unread'}):\n"
        + (record.content or "")[:800]
        + ("\n[Excerpt; retrieve the full publication.]" if len(record.content or "") > 800 else "")
        for record in records[:20]
    ]
    if len(records) > 20:
        entries.append("More pending deliveries remain.")
    entries.append(
        "Retrieve full durable messages with meiva.ui.publications(pending_only=True, after_id=0, limit=50); continue after the last returned id."
    )
    return "\n\n".join(entries)
