# meiva:note
# The system notes a task operation delivered, read back from the inbound rows it committed after
# the recording started.
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg

from base.db import fetch_one


@dataclass(frozen=True)
class Note:
    recipient: int
    content: str
    resurrect: bool | None
    task_id: int | None


class TaskNotes:
    def __init__(self, conn: psycopg.Connection, after: int) -> None:
        self.conn = conn
        self.after = after

    def restart(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(id), 0) FROM inbound_messages")
            self.after = int(fetch_one(cur, "note baseline")[0])

    def deliveries(self) -> list[tuple[int, str]]:
        return [(note.recipient, note.content) for note in self.read()]

    def read(self) -> list[Note]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT agent_id, content, payload FROM inbound_messages "
                "WHERE id > %s AND kind='system_note' ORDER BY id",
                (self.after,),
            )
            rows = cur.fetchall()
        return [
            Note(recipient, content, payload.get("delivery_resurrect"), payload.get("task_id"))
            for recipient, content, payload in rows
        ]


@contextmanager
def record_notes(conn: psycopg.Connection) -> Generator[TaskNotes]:
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(id), 0) FROM inbound_messages")
        after = int(fetch_one(cur, "note baseline")[0])
    yield TaskNotes(conn, after)
