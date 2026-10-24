from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import psutil
import psycopg
import pytest
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from base.api_contracts.agent_contract import ShellSessionKillTiming
from base.config import settings
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from base.sessions.backend import PtySessionBackend
from base.sessions.pty.client import PtyClientConfig
from base.sessions.pty.tests.pty_reaper import PtyReaper
from base.sessions.pty.tests.pty_reaper import pty_reaper as pty_reaper
from base.sessions.pty.tests.pty_service import pty_service as pty_service
from base.sessions.pty.tests.pty_service import service_socket
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from ops.lifecycle import termination
from ops.lifecycle.tests.loopback.agent_host import ServedAgentHost, serve_agent_host
from ops.lifecycle.tests.termination_faults import reject_chat, reject_pair_after_insert
from ops.rpc_schemas import TerminateAgentRequest


@pytest.fixture
def running_agent_id(db_conn: psycopg.Connection) -> int:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta (id,status,machine) VALUES (%s,'running','test-machine')",
        (agent_id,),
    )
    db_conn.commit()
    return agent_id


@pytest.fixture
def db_pool(db_url: str) -> Iterator[ConnectionPool]:
    with ConnectionPool(
        settings.data_plane.db_url,
        min_size=1,
        max_size=1,
        kwargs={"prepare_threshold": None},
    ) as pool:
        yield cast(ConnectionPool, pool)


class TestTerminateAgentRequestMessage:
    def test_normalizes_non_empty_message(self) -> None:
        body = TerminateAgentRequest(message="  leave the findings in the log  ")
        assert body.message == "leave the findings in the log"

    @pytest.mark.parametrize("message", ["", "   ", "x" * 1_000_001])
    def test_rejects_invalid_message(self, message: str) -> None:
        with pytest.raises(ValidationError):
            TerminateAgentRequest(message=message)

    def test_message_requires_a_chat_source(self) -> None:
        with pytest.raises(ValidationError, match="Unrecognized inbound source"):
            TerminateAgentRequest(message="final note", source="machine-pause")


def test_termination_inbounds_are_atomic_and_fall_back_to_pending_message(
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    running_agent_id: int,
    database: Database,
    event_bus: EventBus,
) -> None:
    with reject_pair_after_insert(db_conn, running_agent_id):
        terminate_id, _cutoff = termination.enqueue_termination_inbounds(
            database,
            event_bus,
            running_agent_id,
            db_pool,
            source="user",
            message="final note",
        )

    rows = db_conn.execute(
        "SELECT id,content,kind,source,status FROM inbound_messages WHERE agent_id=%s ORDER BY id",
        (running_agent_id,),
    ).fetchall()
    assert rows == [
        (terminate_id, "", "terminate", "user", "pending"),
        (rows[1][0], "final note", "chat", "user", "pending"),
    ]


def test_termination_survives_failed_message_retry(
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    running_agent_id: int,
    database: Database,
    event_bus: EventBus,
) -> None:
    with reject_chat(db_conn, running_agent_id):
        terminate_id, _cutoff = termination.enqueue_termination_inbounds(
            database,
            event_bus,
            running_agent_id,
            db_pool,
            source="user",
            message="final note",
        )

    assert db_conn.execute(
        "SELECT id,content,kind,status FROM inbound_messages WHERE agent_id=%s",
        (running_agent_id,),
    ).fetchall() == [(terminate_id, "", "terminate", "pending")]


def test_force_termination_retries_command_before_message(
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    running_agent_id: int,
) -> None:
    with reject_pair_after_insert(db_conn, running_agent_id):
        old_status, _, terminate_id, _cutoff = termination.force_terminate_transaction(
            running_agent_id,
            db_pool,
            source="user",
            message="final force note",
        )

    rows = db_conn.execute(
        "SELECT id,content,kind,status FROM inbound_messages WHERE agent_id=%s ORDER BY id",
        (running_agent_id,),
    ).fetchall()
    assert old_status.value == "running"
    assert rows == [
        (terminate_id, "", "terminate", "done"),
        (rows[1][0], "final force note", "chat", "pending"),
    ]
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (running_agent_id,)
    ).fetchone() == ("terminated",)


def _terminate_payloads(db_conn: psycopg.Connection, agent_id: int) -> list[object]:
    return [
        row[0]
        for row in db_conn.execute(
            "SELECT payload FROM inbound_messages WHERE agent_id=%s AND kind='terminate' "
            "ORDER BY id",
            (agent_id,),
        ).fetchall()
    ]


async def _until_blocked_or_done(task: asyncio.Task[object], holder_pid: int) -> None:
    deadline = asyncio.get_running_loop().time() + 10
    while not task.done():
        with psycopg.connect(settings.data_plane.db_url, autocommit=True) as probe:
            row = probe.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity "
                "WHERE %s = ANY(pg_blocking_pids(pid)))",
                (holder_pid,),
            ).fetchone()
        if row == (True,):
            return
        assert asyncio.get_running_loop().time() < deadline, "enqueue neither blocked nor ended"
        await asyncio.sleep(0.02)


# meiva:note
# One opened shell session: its backend name and the pid of its shell.
@dataclass(frozen=True)
class OpenedShell:
    name: str
    pid: int


# meiva:note
# Real shell sessions of one agent, created in the test's own pty-sessions service.
#
# `open` creates the persistent shell the agent's `shell-<id>` naming rule describes (with an
# optional `-<name>` suffix, as a watcher has) and pins its process so the test cleans it up
# whatever the outcome. `alive` lists the sessions the service still holds.
class Shells:
    def __init__(self, backend: PtySessionBackend, reaper: PtyReaper, home: Path) -> None:
        self._backend = backend
        self._reaper = reaper
        self._home = home

    def open(
        self, agent_id: int, shell_id: int, *, command: str = "", suffix: str = ""
    ) -> OpenedShell:
        from base.cluster import session_name

        return self.open_named(session_name(f"agent-{agent_id}-shell-{shell_id}") + suffix, command)

    def open_named(self, name: str, command: str) -> OpenedShell:
        assert self._backend.new_session(
            name, command, self._home, env={"MEIVA_HOME": str(self._home)}
        )
        return OpenedShell(name, self._reaper.track_session(name).pid)

    def alive(self) -> set[str]:
        return set(self._backend.list_sessions())


@pytest.fixture
def shells(pty_service: object, pty_reaper: PtyReaper, unit_home: Path) -> Shells:
    return Shells(
        PtySessionBackend(config=PtyClientConfig(socket_path=service_socket())),
        pty_reaper,
        unit_home,
    )


def _set_session_index(db_conn: psycopg.Connection, agent_id: int, session_index: int) -> None:
    db_conn.execute(
        "UPDATE agents_meta SET session_index=%s WHERE id=%s", (session_index, agent_id)
    )
    db_conn.commit()


class TestKillAllShellSessions:
    async def test_graceful_live_agent_defers_the_kill_to_exit(
        self,
        db_conn: psycopg.Connection,
        db_pool: ConnectionPool,
        running_agent_id: int,
        shells: Shells,
        agent_host: ServedAgentHost,
        database: Database,
        event_bus: EventBus,
        emitted_events: EmittedEvents,
    ) -> None:
        from ops import lifecycle
        from ops.rpc_schemas.terminate import ShellSessionsKill

        shell = shells.open(running_agent_id, 0).name
        _set_session_index(db_conn, running_agent_id, 1)

        resp = await lifecycle.terminate_agent_op(
            database,
            event_bus,
            running_agent_id,
            TerminateAgentRequest(kill_all_shell_sessions=True),
            db_pool,
            host=agent_host.host,
        )

        assert resp.status == "enqueued"
        assert resp.shell_sessions == ShellSessionsKill(
            when=ShellSessionKillTiming.AT_EXIT, killed=[]
        )
        assert shells.alive() == {shell}
        assert _terminate_payloads(db_conn, running_agent_id) == [{"kill_all_shell_sessions": True}]
        events = list(emitted_events)
        assert [event.attributes for event in events] == [
            {"inbound_id": events[0].attributes["inbound_id"], "kill_all_shell_sessions": True}
        ]

    async def test_without_the_option_nothing_is_recorded_or_killed(
        self,
        db_conn: psycopg.Connection,
        db_pool: ConnectionPool,
        running_agent_id: int,
        shells: Shells,
        agent_host: ServedAgentHost,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        from ops import lifecycle

        shell = shells.open(running_agent_id, 0).name
        _set_session_index(db_conn, running_agent_id, 1)

        graceful = await lifecycle.terminate_agent_op(
            database,
            event_bus,
            running_agent_id,
            TerminateAgentRequest(),
            db_pool,
            host=agent_host.host,
        )
        forced = await lifecycle.terminate_agent_op(
            database,
            event_bus,
            running_agent_id,
            TerminateAgentRequest(force=True),
            db_pool,
            host=agent_host.host,
        )

        assert (graceful.status, graceful.shell_sessions) == ("enqueued", None)
        assert (forced.status, forced.shell_sessions) == ("enqueued", None)
        assert shells.alive() == {shell}
        payloads = _terminate_payloads(db_conn, running_agent_id)
        assert len(payloads) == 2
        assert all(
            not isinstance(payload, dict) or "kill_all_shell_sessions" not in payload
            for payload in payloads
        )

    async def test_force_kills_right_away_and_reports_the_ids(
        self,
        db_conn: psycopg.Connection,
        db_pool: ConnectionPool,
        running_agent_id: int,
        shells: Shells,
        agent_host: ServedAgentHost,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        from ops import lifecycle
        from ops.rpc_schemas.terminate import ShellSessionsKill

        shells.open(running_agent_id, 0)
        shells.open(running_agent_id, 3)
        _set_session_index(db_conn, running_agent_id, 4)

        resp = await lifecycle.terminate_agent_op(
            database,
            event_bus,
            running_agent_id,
            TerminateAgentRequest(force=True, kill_all_shell_sessions=True),
            db_pool,
            host=agent_host.host,
        )

        assert resp.status == "enqueued"
        assert resp.shell_sessions == ShellSessionsKill(
            when=ShellSessionKillTiming.NOW, killed=[0, 3]
        )
        assert shells.alive() == set()
        assert agent_host.cancel_requests == [running_agent_id]
        assert _terminate_payloads(db_conn, running_agent_id) == [{"kill_all_shell_sessions": True}]

    async def test_force_supersedes_a_graceful_kill_request_without_carrying_it(
        self,
        db_conn: psycopg.Connection,
        db_pool: ConnectionPool,
        running_agent_id: int,
        shells: Shells,
        agent_host: ServedAgentHost,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        from ops import lifecycle

        shell = shells.open(running_agent_id, 0).name
        _set_session_index(db_conn, running_agent_id, 1)
        await lifecycle.terminate_agent_op(
            database,
            event_bus,
            running_agent_id,
            TerminateAgentRequest(kill_all_shell_sessions=True),
            db_pool,
            host=agent_host.host,
        )
        resp = await lifecycle.terminate_agent_op(
            database,
            event_bus,
            running_agent_id,
            TerminateAgentRequest(force=True),
            db_pool,
            host=agent_host.host,
        )

        assert resp.shell_sessions is None
        assert shells.alive() == {shell}

    @pytest.mark.parametrize("became_terminated", ["inserted", "committed_before_the_request"])
    async def test_an_already_terminated_agent_is_killed_now(
        self,
        became_terminated: str,
        db_conn: psycopg.Connection,
        db_pool: ConnectionPool,
        shells: Shells,
        agent_host: ServedAgentHost,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        from ops import lifecycle
        from ops.rpc_schemas.terminate import ShellSessionsKill

        agent_id = create_agent(db_conn)
        db_conn.execute(
            "INSERT INTO agents_meta (id,status,machine,termination_source,session_index) "
            "VALUES (%s,%s,'test-machine','exit',4)",
            (agent_id, "terminated" if became_terminated == "inserted" else "running"),
        )
        if became_terminated != "inserted":
            db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (agent_id,))
        db_conn.commit()
        shells.open(agent_id, 0)
        shells.open(agent_id, 3)

        resp = await lifecycle.terminate_agent_op(
            database,
            event_bus,
            agent_id,
            TerminateAgentRequest(kill_all_shell_sessions=True),
            db_pool,
            host=agent_host.host,
        )

        assert resp.status == "already_terminated"
        assert resp.shell_sessions == ShellSessionsKill(
            when=ShellSessionKillTiming.NOW, killed=[0, 3]
        )
        assert shells.alive() == set()
        assert agent_host.cancel_requests == []
        assert _terminate_payloads(db_conn, agent_id) == []

    async def test_enqueue_waits_on_the_row_lock_of_a_committing_death(
        self,
        db_conn: psycopg.Connection,
        db_pool: ConnectionPool,
        running_agent_id: int,
        shells: Shells,
        agent_host: ServedAgentHost,
        database: Database,
        event_bus: EventBus,
    ) -> None:
        from ops import lifecycle
        from ops.rpc_schemas.terminate import ShellSessionsKill

        shells.open(running_agent_id, 0)
        shells.open(running_agent_id, 3)
        _set_session_index(db_conn, running_agent_id, 4)

        with psycopg.connect(settings.data_plane.db_url) as apply_conn:
            apply_conn.execute(
                "SELECT 1 FROM agents_meta WHERE id=%s FOR UPDATE", (running_agent_id,)
            )
            op = asyncio.create_task(
                lifecycle.terminate_agent_op(
                    database,
                    event_bus,
                    running_agent_id,
                    TerminateAgentRequest(kill_all_shell_sessions=True),
                    db_pool,
                    host=agent_host.host,
                )
            )
            await _until_blocked_or_done(op, apply_conn.info.backend_pid)
            apply_conn.execute(
                "UPDATE agents_meta SET status='terminated', termination_source='user' WHERE id=%s",
                (running_agent_id,),
            )
            apply_conn.commit()
            resp = await op

        assert resp.status == "already_terminated"
        assert resp.shell_sessions == ShellSessionsKill(
            when=ShellSessionKillTiming.NOW, killed=[0, 3]
        )
        assert shells.alive() == set()
        assert _terminate_payloads(db_conn, running_agent_id) == []


# meiva:note
# A force terminate kills the owner's older shells and watchers, and only those. A page session, a
# foreign agent's shell and a shell allocated after the cutoff was taken survive. The late shell is
# allocated while the agent host is cancelling the turn, after the force fence committed.
async def test_kill_terminates_only_the_owners_real_shell_sessions(
    db_conn: psycopg.Connection,
    db_pool: ConnectionPool,
    running_agent_id: int,
    unit_home: Path,
    shells: Shells,
    pty_reaper: PtyReaper,
    database: Database,
    event_bus: EventBus,
) -> None:
    import shlex

    from base.sessions.page_session import page_session_name
    from ops import lifecycle
    from ops.rpc_schemas.terminate import ShellSessionsKill

    marker = unit_home / "notice.marker"
    other_agent_id = create_agent(db_conn)
    watcher_cmd = shlex.join(
        [
            sys.executable,
            "-u",
            str(Path(__file__).with_name("_notice_on_exit_child.py")),
            str(marker),
        ]
    )
    shell = shells.open(running_agent_id, 0)
    watcher = shells.open(running_agent_id, 1, command=watcher_cmd, suffix="-watcher")
    page = shells.open_named(page_session_name(running_agent_id, "dash_board", 2), "sleep 300")
    foreign = shells.open(other_agent_id, 0)
    job_pid = _wait_child(watcher.pid)
    pty_reaper.track(psutil.Process(job_pid))
    _set_session_index(db_conn, running_agent_id, 3)
    late: list[OpenedShell] = []

    def allocate_after_acceptance(agent_id: int) -> None:
        with psycopg.connect(settings.data_plane.db_url) as allocator:
            row = allocator.execute(
                "UPDATE agents_meta SET session_index=session_index+1 WHERE id=%s "
                "RETURNING session_index-1",
                (agent_id,),
            ).fetchone()
        assert row == (3,)
        late.append(shells.open(agent_id, 3, suffix="-replacement"))

    with serve_agent_host(unit_home, on_cancel=allocate_after_acceptance) as served:
        resp = await lifecycle.terminate_agent_op(
            database,
            event_bus,
            running_agent_id,
            TerminateAgentRequest(force=True, kill_all_shell_sessions=True),
            db_pool,
            host=served.host,
        )

    assert resp.shell_sessions == ShellSessionsKill(when=ShellSessionKillTiming.NOW, killed=[0, 1])
    assert shells.alive() == {page.name, foreign.name, *(opened.name for opened in late)}
    assert not _pid_alive(shell.pid) and not _pid_alive(watcher.pid)
    assert not _pid_alive(job_pid)
    assert _pid_alive(page.pid) and _pid_alive(foreign.pid)
    assert all(_pid_alive(opened.pid) for opened in late)
    assert not marker.exists()


def _wait_child(pid: int, timeout: float = 15.0) -> int:
    import time

    import psutil

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        children = psutil.Process(pid).children(recursive=True)
        if children:
            return children[0].pid
        time.sleep(0.05)
    raise AssertionError(f"shell {pid} never started its job")


def _pid_alive(pid: int, timeout: float = 5.0) -> bool:
    import time

    import psutil

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return False
        except psutil.NoSuchProcess:
            return False
        time.sleep(0.05)
    return True
