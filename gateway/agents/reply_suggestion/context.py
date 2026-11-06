# meiva:note
# Bounded, provenance-bearing inputs for user reply drafting across agents.

import json
from dataclasses import dataclass

from psycopg import Connection
from psycopg.rows import dict_row
from pydantic import TypeAdapter


@dataclass(frozen=True)
class SuggestionContext:
    parts: tuple[tuple[str, str], ...]
    has_prompt: bool
    has_messages: bool

    @property
    def text(self) -> str:
        return "\n\n".join(f"[{label}]\n{text}" for label, text in self.parts)


def read_context(conn: Connection, agent_id: int) -> SuggestionContext:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id,content,suggestions FROM agent_user_messages WHERE agent_id=%s "
            "AND content IS NOT NULL ORDER BY id DESC LIMIT 5",
            (agent_id,),
        )
        publications = cur.fetchall()
        cur.execute(
            "SELECT id,title,content,question FROM agent_notices WHERE agent_id=%s "
            "AND require_response AND resolved_at IS NULL ORDER BY id DESC LIMIT 5",
            (agent_id,),
        )
        questions = cur.fetchall()
        cur.execute(
            "SELECT f.id,o.agent_id,o.context,f.candidate,f.outcome,f.sent_text "
            "FROM reply_suggestion_feedback f JOIN reply_suggestion_offers o ON o.id=f.offer_id "
            "ORDER BY f.id DESC LIMIT 20"
        )
        feedback = cur.fetchall()
    candidates: list[str] = []
    for row in publications:
        candidates.extend(TypeAdapter(list[str]).validate_python(row["suggestions"]))
        row["content"] = row["content"][:1600]
    for row in questions:
        row["content"] = (row["content"] or "")[:1600]
    for row in feedback:
        row["context"] = row["context"][:800]
        row["sent_text"] = (row["sent_text"] or "")[:1600]
    parts = [
        (label, json.dumps(list(reversed(rows)), ensure_ascii=False))
        for label, rows in [
            ("Formal messages", publications),
            ("Open questions", questions),
            ("Explicit user feedback across agents", feedback),
        ]
        if rows
    ]
    return SuggestionContext(
        tuple(parts), bool(questions or candidates), bool(publications or questions)
    )
