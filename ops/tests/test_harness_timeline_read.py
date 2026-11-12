# meiva:note
# The `harness_timeline_read` operation on a real record: the session record names the session and its working
# directory, the file lives where the session's own Claude home puts it, and the inbox rows claimed around the
# time the rows were written say who sent what. A session with no record, or no file yet, has an empty timeline.
# meiva:link docs/decisions/agents/graph/2026-11-13-timeline-read-from-the-record.md

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.db import create_agent
from base.lm.harness.registry import FileSessionRegistry, SessionRecord
from base.paths import harness_sessions_dir, workspace_dir
from ops.harness_timeline import read_harness_timeline
from ops.rpc_schemas import HarnessTimelineReadRequest

FIXTURE = (
    Path(__file__).parents[2] / "base/lm/harness/claude/transcript/tests/fixtures/basic/main.jsonl"
)
SESSION = "5914ebdf-bbec-47c8-bcd6-5a6da081f08a"


@pytest.fixture
def pool(db_url: str) -> Iterator[ConnectionPool]:
    pool: ConnectionPool = ConnectionPool(db_url, min_size=1, max_size=1)
    with pool:
        yield pool


def _request(agent: int, **fields: object) -> HarnessTimelineReadRequest:
    base: dict[str, object] = {
        "agent_id": agent,
        "harness": "claude_code",
        "limit": 50,
        "before": None,
        "explicit_visibility": False,
        "delivery_mode": "naive",
    }
    return HarnessTimelineReadRequest.model_validate({**base, **fields})


def _session(tmp_path: Path, agent: int, *, write_file: bool) -> Path:
    home = tmp_path / "home"
    workspace = workspace_dir(agent, home=home)
    FileSessionRegistry(harness_sessions_dir(home=home)).save(
        SessionRecord(
            agent_id=agent,
            session_id=SESSION,
            cwd=str(workspace),
            model="m",
            created_at="2026-11-12T08:00:00+00:00",
            updated_at="2026-11-12T08:00:00+00:00",
            harness="claude_code",
            generation=1,
        )
    )
    if write_file:
        folder = tmp_path / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
        folder.mkdir(parents=True)
        (folder / f"{SESSION}.jsonl").write_text(FIXTURE.read_text())
    return home


def _claimed(conn: psycopg.Connection, agent: int, content: str, key: str) -> int:
    row = conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, status, created_at, claimed_at, payload) "
        "VALUES (%s, %s, 'chat', 'user', 'done', '2026-11-12T08:56:50Z', '2026-11-12T08:56:58Z', %s::jsonb) RETURNING id",
        (agent, content, f'{{"harness_input_key": "{key}"}}'),
    ).fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def test_a_real_record_is_projected_with_the_inbox_rows_that_sent_its_input(
    tmp_path: Path, db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    agent = create_agent(db_conn)
    first = _claimed(db_conn, agent, "Think briefly, then use Bash", "k1")
    second = _claimed(db_conn, agent, "and tell me the output", "k1")
    home = _session(tmp_path, agent, write_file=True)

    window = read_harness_timeline(
        _request(agent), pool=pool, home=home, environ={"HOME": str(tmp_path)}
    )

    kinds = [item.kind for item in window.items]
    assert kinds == [
        "inbound_chat",
        "inbound_chat",
        "agent_reasoning",
        "agent_code",
        "code_output",
        "agent_reasoning",
        "agent_chat",
    ]
    assert [item.inbound_id for item in window.items[:2]] == [first, second]
    assert window.size == len(FIXTURE.read_bytes()) and not window.has_more


def test_a_session_with_no_record_or_no_file_yet_has_an_empty_timeline(
    tmp_path: Path, db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    agent = create_agent(db_conn)
    environ = {"HOME": str(tmp_path)}
    none = read_harness_timeline(
        _request(agent), pool=pool, home=tmp_path / "home", environ=environ
    )
    assert (none.items, none.has_more, none.size) == ([], False, 0)
    home = _session(tmp_path, agent, write_file=False)
    unwritten = read_harness_timeline(_request(agent), pool=pool, home=home, environ=environ)
    assert (unwritten.items, unwritten.has_more, unwritten.size) == ([], False, 0)


def test_a_page_before_an_item_ends_at_that_row(
    tmp_path: Path, db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    agent = create_agent(db_conn)
    home = _session(tmp_path, agent, write_file=True)
    environ = {"HOME": str(tmp_path)}
    head = read_harness_timeline(_request(agent, limit=2), pool=pool, home=home, environ=environ)
    oldest = int(head.items[0].item_id.removeprefix("h").split(".")[0])
    page = read_harness_timeline(
        _request(agent, limit=2, before=oldest), pool=pool, home=home, environ=environ
    )
    assert head.has_more
    assert all(int(i.item_id.removeprefix("h").split(".")[0]) < oldest for i in page.items)


def test_a_harness_whose_timeline_is_stored_is_refused(
    tmp_path: Path, db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    agent = create_agent(db_conn)
    with pytest.raises(ValueError, match="not read from its record"):
        _ = read_harness_timeline(
            _request(agent, harness="codex"), pool=pool, home=tmp_path, environ={}
        )
