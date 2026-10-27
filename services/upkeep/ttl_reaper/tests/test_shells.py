# meiva:note
# Shell reclamation against the real Postgres fixture and a real runner listener: expired shell
# rows dispatch a shell_kill op to their home machine and are deleted only on a definitive verdict
# (killed / absent / machine_absent). Unreachable machines, failed ops, malformed answers and
# unknown machines leave the row for the next round. Owners are notified only when the reap
# interrupted a running job; an idle or already-absent shell's reaping is silent. A renewal between
# the select and the dispatch wins.
#
# The stale sweep takes the rows the deadline path never reaches: one shell_probe per
# (machine, agent) lists the host's live sessions and the rows of the sessions it did not list are
# dropped — but only on a definitive answer, never for a row younger than the grace, and never for
# a row rewritten since the select.

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.api_contracts.agent_contract import ShellKillMode
from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.daemon.loop_health import LoopProgress
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from ops.rpc_schemas import ShellInfo, ShellKillResult, ShellProbeResult
from services.upkeep.ttl_reaper import shells
from services.upkeep.ttl_reaper.config import TtlReaperConfig
from services.upkeep.ttl_reaper.remote import remote_round
from services.upkeep.ttl_reaper.shells import (
    claim_shell_row_still_expired,
    delete_stale_row,
    reap_expired_shells,
    reap_stale_shells,
    stale_shell_rows_blocking,
)
from services.upkeep.ttl_reaper.tests.runner_stub import (
    Reply,
    completed,
    failed,
    refused,
    register_url,
    runner,
)
from services.upkeep.ttl_reaper.tests.slices import reaper_config


def _progress() -> LoopProgress:
    return LoopProgress("test", 60.0, timers=SYSTEM_TIMERS)


async def _reap(
    pool: ConnectionPool,
    *,
    database_gate: ProcessDbGate,
    bus: EventBus | None = None,
    config: TtlReaperConfig | None = None,
) -> list[tuple[int, int]]:
    return await reap_expired_shells(
        pool,
        Database.from_settings(gate=database_gate),
        bus or EventBus.from_settings(rng=seeded_rng()),
        _progress(),
        config or reaper_config(),
    )


def _killed(*, interrupted: bool, name: str | None = None) -> Reply:
    return completed(
        ShellKillResult(mode=ShellKillMode.KILLED, interrupted=interrupted, name=name).model_dump(
            mode="json"
        )
    )


@pytest.fixture()
def reaper_pool(db_url: str, *, database_gate: ProcessDbGate) -> Iterator[ConnectionPool]:
    """The reaper's own small pool (the functions take a ConnectionPool)."""
    import base.db

    pool = base.db.pool(max_size=2, gate=database_gate)
    yield pool
    pool.close()


def _running_agent(conn: psycopg.Connection, *, machine: str = "unknown") -> int:
    aid = create_agent(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status, machine) "
            "VALUES (%s, 'user', 'running', %s)",
            (aid, machine),
        )
    conn.commit()
    return aid


def _system_inbounds(conn: psycopg.Connection, agent_id: int) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT content FROM inbound_messages WHERE agent_id = %s AND source = 'system'",
            (agent_id,),
        )
        return [r[0] for r in cur.fetchall()]


def _insert_shell_row(
    db_conn: psycopg.Connection,
    agent_id: int,
    session_id: int,
    *,
    expires_at: datetime,
    lived: timedelta = timedelta(hours=1),
    created_at: datetime | None = None,
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at, created_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (agent_id, session_id) DO UPDATE SET expires_at = EXCLUDED.expires_at",
            (agent_id, session_id, expires_at, created_at or expires_at - lived),
        )
    db_conn.commit()


# meiva:note
# A deadline far out (the stale sweep's business, never the kill path's) and a birth past the
# sweep's grace window.
def _tracked() -> datetime:
    return datetime.now(UTC) + timedelta(days=365)


def _past_the_grace() -> datetime:
    return datetime.now(UTC) - timedelta(hours=7)


async def _sweep(
    pool: ConnectionPool,
    *,
    database_gate: ProcessDbGate,
    config: TtlReaperConfig | None = None,
) -> list[tuple[int, int]]:
    return await reap_stale_shells(
        pool, Database.from_settings(gate=database_gate), _progress(), config or reaper_config()
    )


def _probed(*session_ids: int) -> Reply:
    return completed(
        ShellProbeResult(shells=[ShellInfo(id=sid) for sid in session_ids]).model_dump(mode="json")
    )


def _expired() -> datetime:
    return datetime.now(UTC) - timedelta(minutes=1)


def _rows(conn: psycopg.Connection, agent_id: int) -> list[int]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT session_id FROM agent_shell_ttls WHERE agent_id = %s ORDER BY session_id",
            (agent_id,),
        ).fetchall()
    ]


async def test_reap_expired_shells_deletes_on_killed(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
) -> None:
    aid = _running_agent(db_conn, machine="macmini")
    _insert_shell_row(db_conn, aid, 3, expires_at=_expired())

    with runner(db_conn, "macmini", lambda _call: _killed(interrupted=True, name="build")) as wire:
        reaped = await _reap(reaper_pool, database_gate=database_gate, bus=event_bus)

    assert reaped == [(aid, 3)]
    assert [(call["kind"], call["payload"]) for call in wire.calls] == [
        ("shell_kill", {"agent_id": aid, "session_id": 3})
    ]
    assert _rows(db_conn, aid) == []
    [message] = _system_inbounds(db_conn, aid)
    assert "Shell session 'build' (id 3, agent" in message
    assert "TTL 1h" in message
    assert "interrupting a running task" in message


async def test_reap_expired_shells_keeps_row_on_unreachable(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, *, database_gate: ProcessDbGate
) -> None:
    aid = _running_agent(db_conn, machine="macmini")
    _insert_shell_row(db_conn, aid, 7, expires_at=_expired())

    with runner(db_conn, "macmini", lambda _call: refused(404)):
        reaped = await _reap(reaper_pool, database_gate=database_gate)

    assert reaped == []
    assert _rows(db_conn, aid) == [7]


async def test_reap_expired_shells_keeps_row_when_the_machine_has_no_address(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, *, database_gate: ProcessDbGate
) -> None:
    aid = _running_agent(db_conn, machine="macmini")
    _insert_shell_row(db_conn, aid, 7, expires_at=_expired())
    register_url(db_conn, "macmini", None)

    assert await _reap(reaper_pool, database_gate=database_gate) == []
    assert _rows(db_conn, aid) == [7]


async def test_reap_expired_shells_keeps_row_when_the_op_fails(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, *, database_gate: ProcessDbGate
) -> None:
    aid = _running_agent(db_conn, machine="macmini")
    _insert_shell_row(db_conn, aid, 7, expires_at=_expired())

    with runner(db_conn, "macmini", lambda _call: failed({"error": "boom"})):
        assert await _reap(reaper_pool, database_gate=database_gate) == []

    assert _rows(db_conn, aid) == [7]


# meiva:note
# Task #4143: a machine absent from the registry is a definitive verdict — the expired row
# is dropped as an absent session instead of being deferred forever, and nothing notifies
# (nothing on a gone machine was interrupted).
async def test_reap_expired_shells_absent_machine_terminalizes_row(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    emitted_events: EmittedEvents,
    *,
    database_gate: ProcessDbGate,
) -> None:
    aid = _running_agent(db_conn, machine="ghost")
    _insert_shell_row(db_conn, aid, 8, expires_at=_expired())

    reaped = await _reap(reaper_pool, database_gate=database_gate)

    assert reaped == [(aid, 8)]
    assert _rows(db_conn, aid) == []
    assert _system_inbounds(db_conn, aid) == []
    # The synthetic verdict is spelled ``machine_absent`` — distinct from the
    # live-host ``absent`` (session already ended) it must not masquerade as.
    assert emitted_events.names()[-1:] == ["shell_ttl_expired"]
    assert emitted_events.attributes("shell_ttl_expired")[-1] == {
        "agent_id": aid,
        "session_id": 8,
        "mode": "machine_absent",
        "interrupted": False,
    }


async def test_reap_expired_shells_keeps_row_on_unknown_machine(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, *, database_gate: ProcessDbGate
) -> None:
    aid = _running_agent(db_conn)
    _insert_shell_row(db_conn, aid, 9, expires_at=_expired())

    assert await _reap(reaper_pool, database_gate=database_gate) == []
    assert _rows(db_conn, aid) == [9]


# meiva:note
# Only a reap that interrupts running work messages the owner; an empty shell and an
# already-gone session are reclaimed without a notice.
@pytest.mark.parametrize(
    "reply",
    [_killed(interrupted=False), completed({"mode": "absent"})],
    ids=["idle-kill", "session-already-gone"],
)
async def test_reap_expired_shells_reaping_without_an_interrupted_job_is_silent(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    reply: Reply,
    *,
    database_gate: ProcessDbGate,
) -> None:
    aid = _running_agent(db_conn, machine="macmini")
    _insert_shell_row(db_conn, aid, 4, expires_at=_expired(), lived=timedelta(minutes=31))

    with runner(db_conn, "macmini", lambda _call: reply):
        reaped = await _reap(reaper_pool, database_gate=database_gate)

    assert reaped == [(aid, 4)]
    assert _rows(db_conn, aid) == []
    assert _system_inbounds(db_conn, aid) == []


# meiva:note
# A shell_kill result missing `interrupted` is malformed: the row stays for the next round
# and nothing is notified.
async def test_reap_expired_shells_result_without_interrupted_is_deferred(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, *, database_gate: ProcessDbGate
) -> None:
    aid = _running_agent(db_conn, machine="macmini")
    _insert_shell_row(db_conn, aid, 6, expires_at=_expired())

    with runner(db_conn, "macmini", lambda _call: completed({"mode": "killed"})):
        reaped = await _reap(reaper_pool, database_gate=database_gate)

    assert reaped == []
    assert _system_inbounds(db_conn, aid) == []
    assert _rows(db_conn, aid) == [6]


# meiva:note
# A watcher (`meiva.watcher.at/cron/launch`) is just an `agent_shell_ttls` row with no
# registry of its own (docs/decisions/2026-09-27-watchers-are-never-restarted.md): a reclaimed
# watcher-shaped session, one that was running a script at its TTL deadline like a real
# watcher always is, gets the same interruption notice any other reclaimed shell with live
# work gets, and exactly once.
async def test_reap_expired_shells_notifies_for_a_watcher_shaped_session(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    *,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
) -> None:
    aid = _running_agent(db_conn, machine="macmini")
    _insert_shell_row(db_conn, aid, 7, expires_at=_expired(), lived=timedelta(minutes=31))

    with runner(db_conn, "macmini", lambda _call: _killed(interrupted=True, name="test-watcher")):
        reaped = await _reap(reaper_pool, database_gate=database_gate, bus=event_bus)

    assert reaped == [(aid, 7)]
    assert _rows(db_conn, aid) == []
    [message] = _system_inbounds(db_conn, aid)
    assert "test-watcher" in message and "interrupting a running task" in message


# meiva:note
# The race the claim closes: both rows of the machine were selected as expired, then the
# owner renewed the second while the first was being killed. The pass must not kill the
# renewed row: its claim fails and the live row survives.
async def test_reap_expired_shells_skips_row_renewed_after_select(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, *, database_gate: ProcessDbGate
) -> None:
    aid = _running_agent(db_conn, machine="macmini")
    _insert_shell_row(db_conn, aid, 43, expires_at=_expired())
    _insert_shell_row(db_conn, aid, 44, expires_at=_expired())

    def renew_the_second_while_the_first_is_killed(call: dict[str, Any]) -> Reply:
        if call["payload"]["session_id"] == 43:
            with psycopg.connect(db_conn.info.dsn) as renewer:
                renewer.execute(
                    "UPDATE agent_shell_ttls SET expires_at = now() + interval '1 hour', "
                    "renewals = renewals + 1 WHERE agent_id = %s AND session_id = 44",
                    (aid,),
                )
        return _killed(interrupted=False)

    with runner(db_conn, "macmini", renew_the_second_while_the_first_is_killed) as wire:
        reaped = await _reap(reaper_pool, database_gate=database_gate)

    assert reaped == [(aid, 43)]
    assert [call["payload"]["session_id"] for call in wire.calls] == [43]
    assert _rows(db_conn, aid) == [44]


async def test_a_dispatch_past_its_deadline_defers_the_row(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool, *, database_gate: ProcessDbGate
) -> None:
    aid = _running_agent(db_conn, machine="macmini")
    _insert_shell_row(db_conn, aid, 5, expires_at=_expired())
    release = threading.Event()

    def hang(_call: dict[str, Any]) -> Reply:
        release.wait(10.0)
        return _killed(interrupted=False)

    try:
        with runner(db_conn, "macmini", hang):
            reaped = await _reap(
                reaper_pool,
                database_gate=database_gate,
                config=reaper_config(shell_kill_deadline_s=0.2),
            )
    finally:
        release.set()

    assert reaped == []
    assert _rows(db_conn, aid) == [5]


async def test_machines_are_reclaimed_concurrently_and_one_machines_rows_in_order(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
) -> None:
    slow = _running_agent(db_conn, machine="slow")
    fast = _running_agent(db_conn, machine="fast")
    for aid, session_id in ((slow, 1), (fast, 2), (slow, 3), (fast, 4)):
        _insert_shell_row(db_conn, aid, session_id, expires_at=_expired())
    release = threading.Event()
    arrivals: list[tuple[str, int]] = []

    def answering(machine: str) -> Any:
        def answer(call: dict[str, Any]) -> Reply:
            arrivals.append((machine, call["payload"]["session_id"]))
            if machine == "slow":
                release.wait(10.0)
            return _killed(interrupted=False)

        return answer

    try:
        with runner(db_conn, "slow", answering("slow")), runner(db_conn, "fast", answering("fast")):
            task = asyncio.create_task(
                _reap(reaper_pool, database_gate=database_gate, bus=event_bus)
            )
            for _ in range(500):
                if _rows(db_conn, fast) == []:
                    break
                await asyncio.sleep(0.01)
                db_conn.rollback()
            assert _rows(db_conn, fast) == []
            assert [session for machine, session in arrivals if machine == "fast"] == [2, 4]
            assert [session for machine, session in arrivals if machine == "slow"] == [1]
            assert _rows(db_conn, slow) == [1, 3]

            release.set()
            reaped = await task
    finally:
        release.set()

    assert sorted(session for _aid, session in reaped) == [1, 2, 3, 4]
    assert [session for machine, session in arrivals if machine == "slow"] == [1, 3]


# meiva:note
# Only local registry evidence proves machine absence; a runner cannot claim it.
@pytest.mark.parametrize(
    "answer", [{"mode": "machine_absent"}, {"mode": "later"}, {"mode": None}, {}]
)
async def test_runner_invalid_mode_preserves_row_without_false_cleanup(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    caplog: pytest.LogCaptureFixture,
    answer: dict[str, object],
    emitted_events: EmittedEvents,
    *,
    database_gate: ProcessDbGate,
) -> None:
    aid = _running_agent(db_conn, machine="macmini")
    _insert_shell_row(db_conn, aid, 3, expires_at=_expired())

    with runner(db_conn, "macmini", lambda _call: completed(answer)):
        assert await _reap(reaper_pool, database_gate=database_gate) == []

    assert _rows(db_conn, aid) == [3]
    assert _system_inbounds(db_conn, aid) == []
    assert "shell_ttl_expired" not in emitted_events.names()
    assert "returned" in caplog.text


async def test_stale_sweep_drops_the_row_of_a_session_the_host_no_longer_lists(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    *,
    database_gate: ProcessDbGate,
) -> None:
    aid = _running_agent(db_conn, machine="macmini")
    _insert_shell_row(db_conn, aid, 1, expires_at=_tracked(), created_at=_past_the_grace())
    _insert_shell_row(db_conn, aid, 2, expires_at=_tracked(), created_at=_past_the_grace())

    with runner(db_conn, "macmini", lambda _call: _probed(1)) as wire:
        dropped = await _sweep(reaper_pool, database_gate=database_gate)

    assert dropped == [(aid, 2)]
    # meiva:note
    # One listing covers every row of that agent — the sweep never asks per row.
    assert [(call["kind"], call["payload"]) for call in wire.calls] == [
        ("shell_probe", {"agent_id": aid})
    ]
    assert _rows(db_conn, aid) == [1]


async def test_stale_sweep_keeps_every_row_when_the_machine_is_unreachable(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    *,
    database_gate: ProcessDbGate,
) -> None:
    aid = _running_agent(db_conn, machine="macmini")
    _insert_shell_row(db_conn, aid, 1, expires_at=_tracked(), created_at=_past_the_grace())

    with runner(db_conn, "macmini", lambda _call: refused(404)):
        dropped = await _sweep(reaper_pool, database_gate=database_gate)

    assert dropped == []
    assert _rows(db_conn, aid) == [1]


async def test_stale_sweep_stops_probing_a_machine_at_the_first_answer_it_does_not_get(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    *,
    database_gate: ProcessDbGate,
) -> None:
    first = _running_agent(db_conn, machine="macmini")
    second = _running_agent(db_conn, machine="macmini")
    _insert_shell_row(db_conn, first, 1, expires_at=_tracked(), created_at=_past_the_grace())
    _insert_shell_row(db_conn, second, 2, expires_at=_tracked(), created_at=_past_the_grace())

    with runner(db_conn, "macmini", lambda _call: refused(404)) as wire:
        dropped = await _sweep(reaper_pool, database_gate=database_gate)

    assert dropped == []
    # meiva:note
    # The host did not answer for the first agent: asking about the second would repeat it.
    assert [call["payload"] for call in wire.calls] == [{"agent_id": first}]
    assert _rows(db_conn, first) == [1]
    assert _rows(db_conn, second) == [2]


async def test_stale_sweep_keeps_every_row_when_the_answer_has_no_listing(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    *,
    database_gate: ProcessDbGate,
) -> None:
    aid = _running_agent(db_conn, machine="macmini")
    _insert_shell_row(db_conn, aid, 1, expires_at=_tracked(), created_at=_past_the_grace())

    with runner(db_conn, "macmini", lambda _call: completed({"unexpected": True})):
        dropped = await _sweep(reaper_pool, database_gate=database_gate)

    assert dropped == []
    assert _rows(db_conn, aid) == [1]


async def test_stale_sweep_ignores_a_row_younger_than_the_grace(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    *,
    database_gate: ProcessDbGate,
) -> None:
    aid = _running_agent(db_conn, machine="macmini")
    _insert_shell_row(
        db_conn,
        aid,
        1,
        expires_at=_tracked(),
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )

    with runner(db_conn, "macmini", lambda _call: _probed()) as wire:
        dropped = await _sweep(reaper_pool, database_gate=database_gate)

    assert dropped == []
    assert _rows(db_conn, aid) == [1]
    assert wire.calls == []


async def test_stale_sweep_drops_rows_of_a_machine_absent_from_the_registry(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    *,
    database_gate: ProcessDbGate,
) -> None:
    aid = _running_agent(db_conn, machine="ghost")
    _insert_shell_row(db_conn, aid, 1, expires_at=_tracked(), created_at=_past_the_grace())

    dropped = await _sweep(reaper_pool, database_gate=database_gate)

    assert dropped == [(aid, 1)]
    assert _rows(db_conn, aid) == []


async def test_a_remote_round_kills_the_expired_row_and_sweeps_the_dead_sessions_row(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
) -> None:
    aid = _running_agent(db_conn, machine="macmini")
    _insert_shell_row(db_conn, aid, 5, expires_at=_expired())
    _insert_shell_row(db_conn, aid, 6, expires_at=_tracked(), created_at=_past_the_grace())

    def answer(call: dict[str, Any]) -> Reply:
        return _killed(interrupted=False) if call["kind"] == "shell_kill" else _probed()

    with runner(db_conn, "macmini", answer) as wire:
        await remote_round(
            reaper_pool,
            Database.from_settings(gate=database_gate),
            event_bus,
            _progress(),
            reaper_config(),
        )

    assert [call["kind"] for call in wire.calls] == ["shell_kill", "shell_probe"]
    assert _rows(db_conn, aid) == []


async def test_a_remote_round_without_its_sweep_leaves_the_rows_alone(
    db_conn: psycopg.Connection,
    reaper_pool: ConnectionPool,
    event_bus: EventBus,
    *,
    database_gate: ProcessDbGate,
) -> None:
    aid = _running_agent(db_conn, machine="macmini")
    _insert_shell_row(db_conn, aid, 1, expires_at=_tracked(), created_at=_past_the_grace())

    with runner(db_conn, "macmini", lambda _call: _probed()) as wire:
        await remote_round(
            reaper_pool,
            Database.from_settings(gate=database_gate),
            event_bus,
            _progress(),
            reaper_config(),
            sweep=False,
        )

    assert wire.calls == []
    assert _rows(db_conn, aid) == [1]


def test_delete_stale_row_leaves_a_row_rewritten_since_the_select(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool
) -> None:
    aid = _running_agent(db_conn, machine="macmini")
    _insert_shell_row(db_conn, aid, 9, expires_at=_tracked(), created_at=_past_the_grace())
    [row] = stale_shell_rows_blocking(reaper_pool)

    # meiva:note
    # The owner renews the session between the select and the delete.
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_shell_ttls SET expires_at = expires_at + interval '1 day' "
            "WHERE agent_id = %s AND session_id = %s",
            (aid, 9),
        )
    db_conn.commit()

    assert not delete_stale_row(reaper_pool, row)
    assert _rows(db_conn, aid) == [9]


def test_claim_still_expired_true_for_expired_row(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool
) -> None:
    aid = _running_agent(db_conn)
    _insert_shell_row(db_conn, aid, 41, expires_at=_expired())
    assert claim_shell_row_still_expired(reaper_pool, aid, 41)


def test_claim_still_expired_false_for_renewed_row(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool
) -> None:
    """A row renewed between the expired select and the kill dispatch fails
    the claim — the deadline moved forward, no kill may be dispatched."""
    aid = _running_agent(db_conn)
    _insert_shell_row(db_conn, aid, 42, expires_at=datetime.now(UTC) + timedelta(hours=1))
    assert not claim_shell_row_still_expired(reaper_pool, aid, 42)


def test_claim_still_expired_evaluates_at_statement_time(
    db_conn: psycopg.Connection,
) -> None:
    """Issue #2053: the claim re-check reads the deadline at statement time.

    A claim transaction that began before the deadline still claims the row
    once the deadline has actually passed; with now() (transaction start) it
    would skip the row and the pass would never dispatch the kill.
    """
    import time

    aid = _running_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at, created_at) "
            "VALUES (%s, %s, clock_timestamp() + interval '1 second', clock_timestamp())",
            (aid, 45),
        )
    db_conn.commit()
    with db_conn.cursor() as cur:
        # The transaction starts before the deadline passes.
        cur.execute("SET TRANSACTION READ WRITE")
        time.sleep(2.0)
        assert shells.claim_still_expired(cur, aid, 45)
    db_conn.rollback()


def test_claim_still_expired_false_for_missing_row(
    db_conn: psycopg.Connection, reaper_pool: ConnectionPool
) -> None:
    aid = _running_agent(db_conn)
    assert not claim_shell_row_still_expired(reaper_pool, aid, 43)
