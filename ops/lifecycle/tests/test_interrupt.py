# meiva:note
# An interrupt: the gateway commits the note, the message and (external harness) the control row
# before anything is interrupted, and the home host's op reports what the interruption did.

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import httpx
import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.api_contracts.agent_contract import AgentNotFound, InterruptResult
from base.cluster import inbound_channel
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from ops.cluster.rpc import RpcClient
from ops.lifecycle import interrupt_hosted_turn_op
from ops.lifecycle.delivery import InterruptDelivery, deliver_interrupt
from ops.lifecycle.hosted_turn import LocalAgentHost
from ops.lifecycle.tests.loopback.agent_host import serve_agent_host
from ops.lifecycle.tests.loopback.scripted_post import Reply, closed_port, serve_post
from ops.rpc_schemas import InterruptAgentRequest

_NOTE = "Your running turn was interrupted by agent:7. Their message follows."


def _agent(db_conn: psycopg.Connection, *, status: str = "running", harness: str = "meiva") -> int:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta (id,status,harness) VALUES (%s,%s,%s)",
        (agent_id, status, harness),
    )
    db_conn.commit()
    return agent_id


def _rows(
    db_conn: psycopg.Connection, agent_id: int
) -> list[tuple[str, str, str, dict[str, object] | None]]:
    rows = db_conn.execute(
        "SELECT kind, source, content, payload FROM inbound_messages WHERE agent_id = %s "
        "ORDER BY id",
        (agent_id,),
    ).fetchall()
    db_conn.commit()
    return rows


@pytest.fixture
def interrupt_pool(db_url: str) -> Iterator[ConnectionPool[psycopg.Connection]]:
    pool: ConnectionPool[psycopg.Connection] = ConnectionPool(
        db_url, min_size=1, max_size=4, kwargs={"prepare_threshold": None}
    )
    with pool:
        yield pool


async def _interrupt(
    pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
    rpc: RpcClient,
    agent_id: int,
    *,
    message: str | None,
    source: str = "agent:7",
) -> InterruptDelivery:
    return await deliver_interrupt(
        pool,
        database,
        event_bus,
        agent_id,
        message=message,
        source=source,
        provenance=None,
        local_machine="test-machine",
        rpc=rpc,
    )


async def test_a_native_interrupt_writes_the_note_then_the_message_and_no_control_row(
    db_conn: psycopg.Connection,
    interrupt_pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
    rpc: RpcClient,
) -> None:
    agent_id = _agent(db_conn)

    delivery = await _interrupt(
        interrupt_pool, database, event_bus, rpc, agent_id, message="stop, the plan changed"
    )

    assert (delivery.running, delivery.external) == (True, False)
    assert _rows(db_conn, agent_id) == [
        ("system_note", "agent:7", _NOTE, {"note_tag": "lifecycle_interrupt"}),
        ("chat", "agent:7", "stop, the plan changed", None),
    ]


async def test_an_external_interrupt_adds_the_control_row_last_with_the_real_source(
    db_conn: psycopg.Connection,
    interrupt_pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
    rpc: RpcClient,
) -> None:
    agent_id = _agent(db_conn, harness="claude_code")

    delivery = await _interrupt(
        interrupt_pool, database, event_bus, rpc, agent_id, message="wait for me"
    )

    assert (delivery.running, delivery.external) == (True, True)
    assert [(kind, source) for kind, source, _c, _p in _rows(db_conn, agent_id)] == [
        ("system_note", "agent:7"),
        ("chat", "agent:7"),
        ("interrupt", "agent:7"),
    ]


@pytest.mark.parametrize("harness", ["meiva", "codex"])
async def test_an_interrupt_of_an_idle_agent_is_a_plain_chat(
    db_conn: psycopg.Connection,
    interrupt_pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
    rpc: RpcClient,
    harness: str,
) -> None:
    agent_id = _agent(db_conn, status="idling", harness=harness)

    delivery = await _interrupt(interrupt_pool, database, event_bus, rpc, agent_id, message="hi")

    assert delivery.running is False
    assert _rows(db_conn, agent_id) == [("chat", "agent:7", "hi", None)]


@pytest.mark.parametrize(
    ("harness", "kinds"),
    [("meiva", ["system_note"]), ("claude_code", ["interrupt", "system_note"])],
)
async def test_the_stop_button_leaves_a_passive_note_and_no_chat(
    db_conn: psycopg.Connection,
    interrupt_pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
    rpc: RpcClient,
    harness: str,
    kinds: list[str],
) -> None:
    agent_id = _agent(db_conn, harness=harness)

    await _interrupt(
        interrupt_pool, database, event_bus, rpc, agent_id, message=None, source="user"
    )

    rows = _rows(db_conn, agent_id)
    assert [kind for kind, _s, _c, _p in rows] == kinds
    assert rows[-1][1:] == (
        "user",
        "Your running turn was interrupted by user.",
        {"note_tag": "lifecycle_interrupt", "passive": True},
    )


@pytest.mark.parametrize(("harness", "woken"), [("meiva", True), ("codex", False)])
async def test_only_a_native_stop_publishes_a_wake_for_its_passive_note(
    db_conn: psycopg.Connection,
    interrupt_pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
    rpc: RpcClient,
    harness: str,
    woken: bool,
) -> None:
    agent_id = _agent(db_conn, harness=harness)
    redis = event_bus.sync_redis(decode_responses=True)
    pubsub: Any = cast("Any", redis).pubsub()
    pubsub.subscribe(inbound_channel(agent_id, event_bus.channel))
    assert pubsub.get_message(timeout=2) is not None

    await _interrupt(
        interrupt_pool, database, event_bus, rpc, agent_id, message=None, source="user"
    )

    woken_ids: set[str] = set()
    while (message := pubsub.get_message(timeout=0.5)) is not None:
        woken_ids.add(str(message["data"]))
    pubsub.close()
    redis.close()
    [note_id] = [
        str(row[0])
        for row in db_conn.execute(
            "SELECT id FROM inbound_messages WHERE agent_id = %s AND kind = 'system_note'",
            (agent_id,),
        ).fetchall()
    ]
    db_conn.commit()
    assert (note_id in woken_ids) is woken


async def test_the_stop_button_on_an_idle_agent_writes_nothing(
    db_conn: psycopg.Connection,
    interrupt_pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
    rpc: RpcClient,
) -> None:
    agent_id = _agent(db_conn, status="idling")

    delivery = await _interrupt(
        interrupt_pool, database, event_bus, rpc, agent_id, message=None, source="user"
    )

    assert delivery.running is False
    assert _rows(db_conn, agent_id) == []


async def test_an_unknown_agent_is_refused_before_anything_is_written(
    interrupt_pool: ConnectionPool, database: Database, event_bus: EventBus, rpc: RpcClient
) -> None:
    with pytest.raises(AgentNotFound):
        await _interrupt(interrupt_pool, database, event_bus, rpc, 987654, message="x")


# meiva:note
# The target row's lock serializes them: each note sits directly in front of its own message.
async def test_two_interrupts_of_one_target_keep_both_messages_in_a_stable_order(
    db_conn: psycopg.Connection,
    interrupt_pool: ConnectionPool,
    database: Database,
    event_bus: EventBus,
    rpc: RpcClient,
) -> None:
    agent_id = _agent(db_conn)

    await asyncio.gather(
        _interrupt(
            interrupt_pool, database, event_bus, rpc, agent_id, message="first", source="agent:1"
        ),
        _interrupt(
            interrupt_pool, database, event_bus, rpc, agent_id, message="second", source="agent:2"
        ),
    )

    rows = [(kind, source, content) for kind, source, content, _p in _rows(db_conn, agent_id)]
    assert len(rows) == 4
    for note, chat in ((rows[0], rows[1]), (rows[2], rows[3])):
        assert note[0] == "system_note" and chat[0] == "chat" and note[1] == chat[1]
    assert {rows[1][2], rows[3][2]} == {"first", "second"}


def test_only_the_user_may_interrupt_without_a_message() -> None:
    assert InterruptAgentRequest(source="user").message is None
    with pytest.raises(ValueError, match="requires a non-empty message"):
        InterruptAgentRequest(source="agent:3")


@pytest.mark.parametrize("message", ["", "   "])
def test_a_blank_message_is_refused_even_from_the_user(message: str) -> None:
    with pytest.raises(ValueError):
        InterruptAgentRequest(source="user", message=message)


@pytest.mark.parametrize(
    ("cancelled", "expected"),
    [(True, InterruptResult.INTERRUPTED), (False, InterruptResult.NOT_RUNNING)],
)
async def test_reports_what_the_host_answered_and_asks_it_to_rearm(
    unit_home: Path, cancelled: bool, expected: InterruptResult
) -> None:
    with serve_agent_host(unit_home, cancelled=cancelled) as served:
        response = await interrupt_hosted_turn_op(12, host=served.host)

    assert response.status is expected
    assert served.cancel_requests == [12]
    assert served.served.posted[0].body["rearm"] is True


async def test_an_unreachable_host_is_an_error_not_a_quiet_not_running(unit_home: Path) -> None:
    host = LocalAgentHost(home=unit_home, health_port=closed_port())

    with pytest.raises(httpx.ConnectError):
        await interrupt_hosted_turn_op(12, host=host)


async def test_a_host_that_answers_with_an_error_status_is_an_error(unit_home: Path) -> None:
    with serve_post({"/cancel-turn": lambda _body: Reply({}, status=503)}) as served:
        host = LocalAgentHost(home=unit_home, health_port=served.port)
        with pytest.raises(httpx.HTTPStatusError):
            await interrupt_hosted_turn_op(12, host=host)


@pytest.mark.parametrize("answer", [{}, {"cancelled": "yes"}])
async def test_a_host_answer_without_a_boolean_cancelled_is_an_error(
    unit_home: Path, answer: dict[str, object]
) -> None:
    with serve_post({"/cancel-turn": lambda _body: Reply(answer)}) as served:
        host = LocalAgentHost(home=unit_home, health_port=served.port)
        with pytest.raises(TypeError, match="cancel-turn answered"):
            await interrupt_hosted_turn_op(12, host=host)
