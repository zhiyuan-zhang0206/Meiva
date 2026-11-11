# meiva:note
# The agent's own open questions as the database holds them: the source every place that shows an agent
# its unanswered questions reads (the pre-compaction reminder, the context after a compaction, an
# external harness after its CLI compacted). One agent id selects the rows, so a fork, which is a new
# agent, never sees the source agent's questions.
# meiva:link meiva_builtins/plugins/meiva_fleet/docs/ask.md

from __future__ import annotations

from psycopg import Connection
from psycopg.rows import class_row

from base.agents.questions.model import OpenQuestion


def read_open_questions(conn: Connection, agent_id: int) -> list[OpenQuestion]:
    with conn.cursor(row_factory=class_row(OpenQuestion)) as cur:
        cur.execute(
            "SELECT id, title, content, question FROM agent_questions "
            "WHERE agent_id=%s AND resolved_at IS NULL ORDER BY id",
            (agent_id,),
        )
        return cur.fetchall()
