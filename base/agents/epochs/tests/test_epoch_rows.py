from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path
from typing import LiteralString, cast

import psycopg
import pytest
from psycopg import sql

from base.agents.epochs.model import RecordExtent
from base.agents.epochs.read import read_epochs
from base.agents.epochs.write import (
    REASON_EXCERPT_CHARS,
    SwitchFacts,
    birth_epoch,
    harness_switch,
    switched_by_of,
)
from base.agents.messages.kwargs import NoteTag
from base.db.rows import IdRow, query_one

_BACKFILL = next(
    Path(__file__)
    .resolve()
    .parents[4]
    .glob("migrations/2026/11/25/*/*_agent-harness-epochs-backfill.sql")
)


def _agent(conn: psycopg.Connection, harness: str = "meiva", overlay: str | None = None) -> int:
    row = query_one(conn, IdRow, "INSERT INTO agents DEFAULT VALUES RETURNING id")
    assert row is not None
    agent_id = row.id
    conn.execute(
        "INSERT INTO agents_meta (id, spawner, status, harness, config_overlay) "
        "VALUES (%s, 'user', 'idling', %s, %s::jsonb)",
        (agent_id, harness, overlay),
    )
    return agent_id


def _run(conn: psycopg.Connection, statements: tuple[tuple[str, tuple[object, ...]], ...]) -> None:
    for statement, params in statements:
        conn.execute(statement, params)  # pyright: ignore[reportArgumentType]


def _note(
    conn: psycopg.Connection, agent_id: int, content: str, at: str, kind: str = "system_note"
) -> None:
    payload = {"note_tag": NoteTag.LIFECYCLE_RESTART.value} if kind == "system_note" else None
    conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, payload, created_at) "
        "VALUES (%s, %s, %s, 'system', %s::jsonb, %s::timestamptz)",
        (agent_id, content, kind, json.dumps(payload) if payload else None, at),
    )


def test_a_switch_closes_the_current_stretch_and_opens_the_next(
    db_conn: psycopg.Connection,
) -> None:
    agent_id = _agent(db_conn)
    db_conn.execute(*birth_epoch(agent_id, harness="meiva", model="deepseek-flash"))
    _run(
        db_conn,
        harness_switch(
            agent_id,
            SwitchFacts(
                new_harness="codex",
                model="gpt-6.2-sol",
                by="user",
                message="  take over the build  ",
                leaving=None,
                arriving=RecordExtent("sess-codex", 0),
            ),
        ),
    )
    _run(
        db_conn,
        harness_switch(
            agent_id,
            SwitchFacts(
                new_harness="meiva",
                model="deepseek-flash",
                by="self",
                message=None,
                leaving=RecordExtent("sess-codex", 4096),
                arriving=None,
            ),
        ),
    )
    _run(
        db_conn,
        harness_switch(
            agent_id,
            SwitchFacts(
                new_harness="codex",
                model="gpt-6.2-sol",
                by="operator",
                message="x" * (REASON_EXCERPT_CHARS + 50),
                leaving=None,
                arriving=RecordExtent("sess-codex", 4096),
            ),
        ),
    )
    db_conn.commit()

    epochs = read_epochs(db_conn, agent_id)
    assert [(e.seq, e.harness, e.switched_by) for e in epochs] == [
        (0, "meiva", "birth"),
        (1, "codex", "user"),
        (2, "meiva", "self"),
        (3, "codex", "operator"),
    ]
    assert [e.ended_at is None for e in epochs] == [False, False, False, True]
    assert epochs[1].source_start == 0
    assert (epochs[1].source_end, epochs[1].session_id) == (4096, "sess-codex")
    assert epochs[3].source_start == 4096
    assert epochs[1].reason == "take over the build"
    assert epochs[2].reason is None
    assert epochs[3].reason == "x" * REASON_EXCERPT_CHARS
    assert all(a.ended_at is not None and a.ended_at <= b.started_at for a, b in pairwise(epochs))
    stored = db_conn.execute(
        "SELECT harness, birth_config -> 'harness_model_selection', harness_configured_at IS NOT NULL "
        "FROM agents_meta WHERE id = %s",
        (agent_id,),
    ).fetchone()
    assert stored == ("codex", 1, True)


def test_two_open_stretches_are_refused(db_conn: psycopg.Connection) -> None:
    agent_id = _agent(db_conn)
    db_conn.execute(*birth_epoch(agent_id, harness="meiva", model=None))
    with pytest.raises(psycopg.errors.UniqueViolation):
        db_conn.execute(*birth_epoch(agent_id, harness="codex", model=None))


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("self", "self"),
        ("agent:7", "self"),
        ("agent:8", "operator"),
        ("user", "user"),
        ("cli", "operator"),
    ],
)
def test_who_asked_for_a_switch(source: str, expected: str) -> None:
    assert switched_by_of(source, 7) == expected


def test_backfill_rebuilds_the_stretches_the_notes_describe(db_conn: psycopg.Connection) -> None:
    plain = _agent(db_conn, "claude_code", '{"llm_model": "claude-fable-5-2"}')
    chain = _agent(db_conn, "codex", '{"llm_model": "gpt-6.2-sol"}')
    broken = _agent(db_conn, "meiva")
    forged = _agent(db_conn, "meiva")
    seeded = _agent(db_conn, "codex")
    _note(
        db_conn,
        chain,
        "[Harness switched from 'meiva' to 'claude_code']\nneeds a shell",
        "2026-11-20 10:00+00",
    )
    _note(db_conn, chain, "[Harness switched from 'claude_code' to 'codex']", "2026-11-21 10:00+00")
    _note(
        db_conn,
        broken,
        "[Harness switched from 'meiva' to 'codex']\nlost track",
        "2026-11-20 10:00+00",
    )
    _note(db_conn, forged, "[Harness switched from 'meiva' to 'bogus']", "2026-11-20 10:00+00")
    _note(
        db_conn,
        forged,
        "[Harness switched from 'meiva' to 'codex']",
        "2026-11-20 11:00+00",
        kind="chat",
    )
    db_conn.execute(*birth_epoch(seeded, harness="codex", model=None))
    db_conn.commit()

    backfill = sql.SQL(cast(LiteralString, _BACKFILL.read_text(encoding="utf-8")))
    db_conn.execute(backfill)
    db_conn.execute(backfill)
    db_conn.commit()

    def shape(agent_id: int) -> list[tuple[int, str, str | None, str, str | None, bool]]:
        return [
            (e.seq, e.harness, e.model, e.switched_by, e.reason, e.ended_at is None)
            for e in read_epochs(db_conn, agent_id)
        ]

    assert shape(plain) == [(0, "claude_code", "claude-fable-5-2", "backfill", None, True)]
    assert shape(chain) == [
        (0, "meiva", None, "backfill", None, False),
        (1, "claude_code", None, "backfill", "needs a shell", False),
        (2, "codex", "gpt-6.2-sol", "backfill", None, True),
    ]
    chain_rows = read_epochs(db_conn, chain)
    assert chain_rows[1].started_at.isoformat().startswith("2026-11-20T10:00")
    assert chain_rows[0].ended_at == chain_rows[1].started_at
    assert shape(broken) == [(0, "meiva", None, "backfill", None, True)]
    assert shape(forged) == [(0, "meiva", None, "backfill", None, True)]
    assert shape(seeded) == [(0, "codex", None, "birth", None, True)]
