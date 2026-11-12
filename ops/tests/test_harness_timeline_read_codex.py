# meiva:note
# The `harness_timeline_read` operation on a Codex thread's rollout: the session record names the thread, the file lives
# under the Codex home the session's own environment names (found by the thread id, whatever the working directory), and
# the inbox rows say who sent what by the `clientUserMessageId` each input carried. A thread with no record, no file yet
# (a thread that has run no turn has none) or a file this reader cannot read has an empty timeline, and the unreadable
# one is told apart by an event.
# meiva:link docs/decisions/agents/graph/2026-11-13-timeline-read-from-the-record.md

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.db import create_agent
from base.lm.harness.registry import FileSessionRegistry, SessionRecord
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.paths import harness_sessions_dir, workspace_dir
from ops.harness_timeline import read_harness_timeline
from ops.rpc_schemas import HarnessTimelineReadRequest

FIXTURE = (
    Path(__file__).parents[2]
    / "base/lm/harness/codex/transcript/tests/fixtures/rollout/session.jsonl"
)
THREAD = "01a1d14b-0f88-7ee3-8636-d3b8c1507a76"
KEYS = [
    "cid-a1-1",
    "cid-a1-2",
    "cid-a1-3",
    "cid-a1-steer",
    "cid-a1-4",
    "cid-a1-5",
    "cid-a1-6",
    "cid-a2-1",
]
ROLLOUT = f"rollout-2026-11-13T03-45-19-{THREAD}.jsonl"


@pytest.fixture
def pool(db_url: str) -> Iterator[ConnectionPool]:
    pool: ConnectionPool = ConnectionPool(db_url, min_size=1, max_size=1)
    with pool:
        yield pool


def _request(agent: int, **fields: object) -> HarnessTimelineReadRequest:
    base: dict[str, object] = {
        "agent_id": agent,
        "harness": "codex",
        "limit": 50,
        "before": None,
        "explicit_visibility": False,
        "delivery_mode": "naive",
    }
    return HarnessTimelineReadRequest.model_validate({**base, **fields})


def _thread(tmp_path: Path, agent: int, *, file_name: str | None) -> Path:
    home = tmp_path / "home"
    FileSessionRegistry(harness_sessions_dir(home=home)).save(
        SessionRecord(
            agent_id=agent,
            session_id=THREAD,
            cwd=str(workspace_dir(agent, home=home)),
            model="m",
            created_at="2026-11-12T19:45:00+00:00",
            updated_at="2026-11-12T19:45:00+00:00",
            harness="codex",
            generation=1,
        )
    )
    if file_name is not None:
        day = tmp_path / "codex/sessions/2026/11/13"
        day.mkdir(parents=True)
        (day / file_name).write_text(FIXTURE.read_text())
    return home


def _environ(tmp_path: Path) -> dict[str, str]:
    return {"HOME": str(tmp_path), "CODEX_HOME": str(tmp_path / "codex")}


def _claimed(conn: psycopg.Connection, agent: int, key: str) -> int:
    row = conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, status, created_at, claimed_at, payload) "
        "VALUES (%s, %s, 'chat', 'user', 'done', '2026-11-12T19:45:00Z', '2026-11-12T19:45:10Z', %s::jsonb) "
        "RETURNING id",
        (agent, f"text of {key}", f'{{"harness_input_key": "{key}"}}'),
    ).fetchone()
    conn.commit()
    assert row is not None
    return int(row[0])


def test_a_real_rollout_is_projected_with_the_inbox_rows_that_sent_its_input(
    tmp_path: Path, db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    agent = create_agent(db_conn)
    ids = [_claimed(db_conn, agent, key) for key in KEYS]
    home = _thread(tmp_path, agent, file_name=ROLLOUT)

    window = read_harness_timeline(
        _request(agent), pool=pool, home=home, environ=_environ(tmp_path)
    )

    inbound = [item for item in window.items if item.kind == "inbound_chat"]
    assert [item.inbound_id for item in inbound] == ids
    assert [item.payload for item in inbound][:2] == ["text of cid-a1-1", "text of cid-a1-2"]
    assert window.items[-1].kind == "agent_chat"
    assert window.size == len(FIXTURE.read_bytes()) and not window.has_more


def test_a_thread_with_no_record_no_file_or_an_unreadable_file_has_an_empty_timeline(
    tmp_path: Path,
    db_conn: psycopg.Connection,
    pool: ConnectionPool,
    loguru_records: LogRecords,
) -> None:
    agent = create_agent(db_conn)
    environ = _environ(tmp_path)
    none = read_harness_timeline(
        _request(agent), pool=pool, home=tmp_path / "home", environ=environ
    )
    assert (none.items, none.has_more, none.size) == ([], False, 0)
    home = _thread(tmp_path, agent, file_name=None)
    unwritten = read_harness_timeline(_request(agent), pool=pool, home=home, environ=environ)
    assert (unwritten.items, unwritten.size) == ([], 0)
    assert loguru_records.events("harness_record_unreadable") == []
    name = f"{ROLLOUT}.zst"
    (tmp_path / "codex/sessions/2026/11/13").mkdir(parents=True)
    (tmp_path / "codex/sessions/2026/11/13" / name).write_text("x")
    packed = read_harness_timeline(_request(agent), pool=pool, home=home, environ=environ)
    assert (packed.items, packed.size) == ([], 0)
    (event,) = loguru_records.events("harness_record_unreadable")
    assert (event["extra"]["session_id"], event["extra"]["name"]) == (THREAD, name)


def test_a_page_before_an_item_ends_at_that_row(
    tmp_path: Path, db_conn: psycopg.Connection, pool: ConnectionPool
) -> None:
    agent = create_agent(db_conn)
    home = _thread(tmp_path, agent, file_name=ROLLOUT)
    environ = _environ(tmp_path)
    head = read_harness_timeline(_request(agent, limit=3), pool=pool, home=home, environ=environ)
    oldest = int(head.items[0].item_id.removeprefix("h").split(".")[0])
    page = read_harness_timeline(
        _request(agent, limit=3, before=oldest), pool=pool, home=home, environ=environ
    )
    assert head.has_more
    assert all(int(i.item_id.removeprefix("h").split(".")[0]) < oldest for i in page.items)
