# meiva:note
# `meiva impersonate relay` through its parser handler, against a real lease and a Codex app
# server on a unix socket: the handler reads the credential from the invocation's stdin, takes
# the Redis address and machine from the invocation's configuration, and runs the relay until
# the lease ends or the host refuses.

from __future__ import annotations

import io
from argparse import Namespace
from dataclasses import replace
from uuid import UUID

import psycopg
import pytest

from base.agents.inbox.insert import insert_inbound_message
from base.db import Database
from base.events.live.bus import EventBus
from cli.commands.agents.relay.push_text import ack_command
from cli.commands.agents.relay.steps import PUSH_MAX_CHARS
from cli.commands.agents.tests.lease_support import ActiveSession, assert_message_pending
from cli.commands.agents.tests.lease_support import active as active
from cli.commands.agents.tests.loopback_hosts import codex_app_server, short_socket_path
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime, Terminal
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime
from cli.parsers import build_parser

THREAD = UUID("b9d32d0d-bd27-40fc-83e8-692769b21523")
TRUNCATION = " (truncated — run the inbox command to read the full body before acknowledging)"


def _relay_handler_args(
    lease: dict[str, object],
    remote: str,
    *,
    runtime: CommandRuntime,
    database: OperatorDatabaseFactory,
) -> Namespace:
    args = build_parser().parse_args(
        [
            "impersonate",
            "relay",
            str(lease["agent_id"]),
            "--lease-id",
            str(lease["id"]),
            "--provider",
            "codex",
            "--thread-id",
            str(THREAD),
            "--codex-remote",
            remote,
            "--debounce",
            "0",
            "--token-stdin",
        ]
    )
    terminal = Terminal(
        io.StringIO(f"{lease['relay_token']}\n"), runtime.terminal.stdout, runtime.terminal.stderr
    )
    args.runtime = replace(runtime, terminal=terminal)
    args.database_factory = database
    return args


@pytest.mark.parametrize("refuse", [False, True])
def test_codex_relay_caps_content_and_preserves_inbox_on_steer_failure(
    refuse: bool,
    active: ActiveSession,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    lease, _, mid = active
    long_id = insert_inbound_message(
        db_conn,
        lease["agent_id"],
        "x" * 5000,
        "user",
        kind="chat",
        bus=event_bus,
        database=database,
    )
    db_conn.execute(
        "UPDATE agent_impersonations SET expires_at=clock_timestamp()+interval '4 seconds' "
        "WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()
    with (
        short_socket_path() as socket_path,
        codex_app_server(socket_path, [True, not refuse]) as host,
    ):
        args = _relay_handler_args(
            lease,
            host.endpoint,
            runtime=command_runtime,
            database=operator_database,
        )
        code = args.func(args)
    assert code == (1 if refuse else 0)
    assert {thread for thread, _ in host.turns} == {str(THREAD)}
    push = host.texts[-1]
    assert "truncated" in push
    assert "before acknowledging" in push
    assert ack_command(lease["session_id"], [mid, long_id], lease["agent_id"]) in push
    body = next(line for line in push.splitlines() if line.startswith("x" * 10))
    assert len(body) <= PUSH_MAX_CHARS + len(TRUNCATION)
    if refuse:
        assert "Codex Steer delivery failed" in capsys.readouterr().err
        assert_message_pending(db_conn, long_id)
