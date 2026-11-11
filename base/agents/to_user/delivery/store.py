from datetime import datetime

from psycopg import Connection, sql
from psycopg.rows import dict_row, scalar_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from base.agents.messages.reply_intent import normalize_last_message_text
from base.api_contracts.notice_question import NoticeQuestion
from base.api_contracts.user_delivery import (
    PublishMessage,
    UserDeliveryMode,
    UserMessage,
)
from base.events.notifications.publish import stage_unread_delivery, stage_unread_seen

COLUMNS = (
    "id, agent_id, mode, source_key, content, suggestions, created_at, "
    "id <= COALESCE((SELECT seen_id FROM agent_user_seen s "
    "WHERE s.agent_id = agent_user_messages.agent_id), 0) AS seen"
)


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
    had_unread = has_unseen(conn, agent_id)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "INSERT INTO agent_user_messages (agent_id, source_key, mode, content, "
            "suggestions, created_at) VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (agent_id, source_key) DO NOTHING",
            (
                agent_id,
                source_key,
                mode.value,
                body.content,
                Jsonb(body.suggestions),
                created_at,
            ),
        )
        inserted = cur.rowcount == 1
        cur.execute(
            sql.SQL(
                "SELECT {} FROM agent_user_messages WHERE agent_id=%s AND source_key=%s"
            ).format(sql.SQL(COLUMNS)),
            (agent_id, source_key),
        )
        message = UserMessage.model_validate(cur.fetchone())
    if (message.content, message.suggestions, message.mode) != (
        body.content,
        body.suggestions,
        mode,
    ):
        raise DeliveryConflictError("this publication key already identifies different content")
    if inserted and body.content is not None:
        stage_unread_delivery(
            conn, agent_id, normalize_last_message_text(body.content), had_unread=had_unread
        )
    return message


def seen_id(conn: Connection, agent_id: int) -> int:
    with conn.cursor(row_factory=scalar_row) as cur:
        cur.execute("SELECT seen_id FROM agent_user_seen WHERE agent_id = %s", (agent_id,))
        value: object = cur.fetchone()
    if value is None:
        return 0
    if not isinstance(value, int):
        raise TypeError("seen cursor must be an integer")
    return value


def messages(
    conn: Connection,
    agent_id: int,
    *,
    pending_only: bool = False,
    after_id: int = 0,
    limit: int | None = None,
) -> list[UserMessage]:
    floor = max(after_id, seen_id(conn, agent_id)) if pending_only else after_id
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            sql.SQL(
                "SELECT {} FROM agent_user_messages WHERE agent_id=%s AND id>%s AND content IS NOT NULL ORDER BY id LIMIT %s"
            ).format(sql.SQL(COLUMNS)),
            (agent_id, floor, limit),
        )
        return [UserMessage.model_validate(row) for row in cur.fetchall()]


def has_unseen(conn: Connection, agent_id: int) -> bool:
    return conn.execute(
        "SELECT EXISTS (SELECT 1 FROM agent_user_messages WHERE agent_id = %s "
        "AND content IS NOT NULL AND id > COALESCE("
        "(SELECT seen_id FROM agent_user_seen WHERE agent_id = %s), 0))",
        (agent_id, agent_id),
    ).fetchone() == (True,)


def advance_seen(conn: Connection, agent_id: int, up_to_id: int) -> tuple[int, bool]:
    newest = conn.execute(
        "SELECT (SELECT COALESCE(MAX(id), 0) FROM agent_user_messages WHERE agent_id = %s), "
        "EXISTS (SELECT 1 FROM agents WHERE id = %s)",
        (agent_id, agent_id),
    ).fetchone()
    if newest is None or not newest[1]:
        raise LookupError(f"agent {agent_id} does not exist")
    if up_to_id > newest[0]:
        raise ValueError(f"up_to_id {up_to_id} is beyond the agent's newest delivery {newest[0]}")
    had_unread = has_unseen(conn, agent_id)
    moved = conn.execute(
        "INSERT INTO agent_user_seen AS s (agent_id, seen_id) VALUES (%s, %s) "
        "ON CONFLICT (agent_id) DO UPDATE SET seen_id = EXCLUDED.seen_id "
        "WHERE s.seen_id < EXCLUDED.seen_id RETURNING seen_id",
        (agent_id, up_to_id),
    ).fetchone()
    if moved is None:
        return seen_id(conn, agent_id), False
    stage_unread_seen(conn, agent_id, had_unread=had_unread, has_unread=has_unseen(conn, agent_id))
    return up_to_id, True


def latest_decision(conn: Connection, agent_id: int) -> int:
    with conn.cursor(row_factory=scalar_row) as cur:
        cur.execute(
            "SELECT (SELECT COALESCE(MAX(id),0) FROM agent_user_messages WHERE agent_id=%s) + "
            "(SELECT COALESCE(MAX(id),0) FROM agent_notices WHERE agent_id=%s)",
            (agent_id, agent_id),
        )
        value: object = cur.fetchone()
    if not isinstance(value, int):
        raise TypeError("delivery aggregate must be an integer")
    return value


def pending_context(records: list[UserMessage]) -> str:
    if not records:
        return ""
    entries = [
        f"Unread publication #{record.id}:\n"
        + (record.content or "")[:800]
        + ("\n[Excerpt.]" if len(record.content or "") > 800 else "")
        for record in records[:20]
    ]
    if len(records) > 20:
        entries.append("More pending deliveries remain.")
    return "\n\n".join(entries)


class PendingQuestion(BaseModel):
    id: int
    title: str
    content: str | None
    question: NoticeQuestion | None


def pending_questions(conn: Connection, agent_id: int) -> str:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id,title,content,question FROM agent_notices WHERE agent_id=%s "
            "AND resolved_at IS NULL ORDER BY id LIMIT 21",
            (agent_id,),
        )
        rows = [PendingQuestion.model_validate(row) for row in cur.fetchall()]
    if not rows:
        return ""
    entries = [
        f"Open Ask #{row.id}: {row.title[:1000]}\n{(row.content or '')[:800]}\nQuestion: "
        + (row.question.model_dump_json()[:1600] if row.question else "text")
        for row in rows[:20]
    ]
    if len(rows) > 20:
        entries.append("More open questions remain.")
    entries.append("meiva.ui.close_question(id) closes one of these questions.")
    return "\n\n".join(entries)
