# meiva:note
# The owner of a closed busy shell session is told, through the database, by the stop itself.

from __future__ import annotations

import shlex
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import psycopg
import pytest

from base.agents.context.clients import DatabaseHandle
from base.db import create_agent
from base.sessions.pty.tests import pty_jobs as jobs
from base.sessions.pty.tests import pty_shells
from base.sessions.pty.tests.pty_reaper import PtyReaper
from base.sessions.pty.tests.pty_reaper import pty_reaper as pty_reaper
from base.sessions.pty.tests.pty_service import service_socket
from base.telemetry import EventPipeline
from cli.commands.lifecycle.stop import do_stop
from cli.commands.lifecycle.tests.stop.stop_support import (
    PtyServiceProcess,
    busy_session,
    stop_environment,
    stop_request,
    written_notices,
)
from cli.commands.lifecycle.tests.stop.stop_support import home as home
from cli.commands.lifecycle.tests.stop.stop_support import pty_service as pty_service
from cli.commands.lifecycle.tests.stop.stop_support import stop_runtime as stop_runtime
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.cli_tests import operator_pipeline as operator_pipeline
from cli.database.tests.command_inputs import command_runtime as command_runtime


def test_busy_fixture_waits_for_the_job_after_a_login_helper(
    home: Path, pty_reaper: PtyReaper, pty_service: PtyServiceProcess
) -> None:
    name = "meiva-agent-987-shell-2044-login-readiness"
    release = home / "release-login-helper"
    startup = home / "login-helper.py"
    startup.write_text(
        "import time\nfrom pathlib import Path\n"
        "print('login-helper-ready', flush=True)\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n",
        encoding="utf-8",
    )
    (home / ".bash_profile").write_text(
        f"{shlex.quote(sys.executable)} -u {shlex.quote(str(startup))}\n", encoding="utf-8"
    )
    done = threading.Event()

    def release_when_ready() -> None:
        # meiva:note
        # The login helper holds the shell until it is released, as soon as it has said it runs.
        while not done.wait(0.02):
            listed = name in {
                info.name for info in pty_shells.client.list_sessions(socket_path=service_socket())
            }
            if listed and "login-helper-ready" in pty_shells.screen(name).splitlines():
                release.touch()
                return

    releaser = threading.Thread(target=release_when_ready)
    releaser.start()
    try:
        busy_session(home, name, jobs.TERM_OK, pty_reaper)
        assert "job-ready" in pty_shells.screen(name).splitlines()
    finally:
        done.set()
        release.touch()
        releaser.join(timeout=5)


def test_stop_records_notice_for_verified_closed_busy_session(
    redis_url: str,
    home: Path,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    stop_runtime: CommandRuntime,
) -> None:
    name = "meiva-agent-987-shell-2044-busy"
    busy_session(home, name, jobs.TERM_OK, pty_reaper)

    rc = do_stop(
        stop_request(timeout=15),
        stop_environment(stop_runtime, operator_database, operator_pipeline),
        retained_children=[],
    )
    assert rc == 0
    (notice,) = written_notices(db_conn)
    assert notice["agent_id"] == 987
    assert notice["session_id"] == 2044
    assert notice["name"] == name
    assert notice["machine"] == stop_runtime.machine.name()
    assert "operator stop" in notice["reason"]


def test_the_stop_writes_the_owners_inbound_to_the_database(
    redis_url: str,
    home: Path,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    stop_runtime: CommandRuntime,
) -> None:
    owner = create_agent(db_conn)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'running')",
            (owner,),
        )
    db_conn.commit()
    name = f"meiva-agent-{owner}-shell-2044-landed"
    busy_session(home, name, jobs.TERM_OK, pty_reaper)

    rc = do_stop(
        stop_request(timeout=15),
        stop_environment(stop_runtime, operator_database, operator_pipeline),
        retained_children=[],
    )
    assert rc == 0
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT source, content, payload->'closure'->>'name' FROM inbound_messages "
            "WHERE agent_id = %s",
            (owner,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    source, content, closed_name = rows[0]
    assert source == "system" and closed_name == name
    assert "closed by an operator stop (meiva stop)" in content


# meiva:note
# A batch that cannot be written is reported with the text its owner would have read, but
# never fails the closure: retrying the whole stop would not restore the resources it closes.
def test_an_unwritable_notice_is_loud_and_does_not_fail_the_stop(
    db_url: str,
    home: Path,
    capsys: pytest.CaptureFixture[str],
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    stop_runtime: CommandRuntime,
) -> None:
    name = "meiva-agent-987-shell-2044-unreachable"
    busy_session(home, name, jobs.TERM_OK, pty_reaper)

    def unreachable() -> DatabaseHandle:
        # meiva:note
        # Nothing listens on port 1: the connection is refused.
        return operator_database.for_url("postgresql://nobody@127.0.0.1:1/meiva")

    rc = do_stop(
        stop_request(timeout=15),
        stop_environment(stop_runtime, unreachable, operator_pipeline),
        retained_children=[],
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert name in err and "agent 987" in err and "could not be written" in err


def test_stop_message_reaches_closed_shell_notice_and_flushes_idle_note_without_live_agent_host(
    redis_url: str,
    home: Path,
    pty_reaper: PtyReaper,
    pty_service: PtyServiceProcess,
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    stop_runtime: CommandRuntime,
) -> None:
    from base.deploy.lifecycle.host_exit_message import (
        consume_host_exit_message,
        write_host_exit_message,
    )

    reason = "Zhiyuan maintenance window"
    busy_owner = create_agent(db_conn)
    idle_owner = create_agent(db_conn)
    machine = stop_runtime.machine.name()
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status, machine) VALUES "
            "(%s, 'user', 'running', %s), (%s, 'user', 'idling', %s)",
            (busy_owner, machine, idle_owner, machine),
        )
    db_conn.commit()
    name = f"meiva-agent-{busy_owner}-shell-2044-explained"
    busy_session(home, name, jobs.TERM_OK, pty_reaper)

    rc = do_stop(
        stop_request(timeout=15, message=reason),
        stop_environment(stop_runtime, operator_database, operator_pipeline),
        retained_children=[],
    )
    assert rc == 0
    assert consume_host_exit_message(home) is None

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT content, payload->'closure'->>'message' FROM inbound_messages "
            "WHERE agent_id = %s AND payload ? 'closure'",
            (busy_owner,),
        )
        closure_rows = cur.fetchall()
        cur.execute(
            "SELECT source, content, payload FROM inbound_messages WHERE agent_id = %s",
            (idle_owner,),
        )
        idle_rows = cur.fetchall()
    assert len(closure_rows) == 1
    closure_content, closure_msg = closure_rows[0]
    assert closure_msg == reason and f"Message: {reason}" in closure_content
    assert len(idle_rows) == 1
    idle_source, idle_content, idle_payload = idle_rows[0]
    assert idle_source == "system:restart"
    assert reason in idle_content
    assert idle_payload == {"note_tag": "lifecycle_restart", "passive": True}

    write_host_exit_message(home, "stale note before force stop")
    forced = do_stop(
        stop_request(timeout=5, force=True, keep_shell_sessions=True),
        stop_environment(stop_runtime, operator_database, operator_pipeline),
        retained_children=[],
    )
    assert forced == 0
    assert consume_host_exit_message(home) is None
