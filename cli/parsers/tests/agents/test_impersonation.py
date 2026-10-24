# meiva:note
# `meiva impersonate`: the explicit controller operations and the request, against a real lease
# in the throwaway database, as the operator's invocation drives them. The command reads and
# moves the lease through its real service, so each case asserts the lease's durable state and
# what the operator was shown; the gateway it speaks to for `send` is a listener on a loopback
# port.


from __future__ import annotations

import io
import json
import re
import stat
import sys
from argparse import Namespace
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest

from base.agents import impersonation as leases
from base.agents.impersonation.host_transport import AntigravityServer, write_antigravity_endpoint
from base.agents.impersonation.status import ImpersonationStatus
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from cli.commands.agents.impersonation import ImpersonationEnv, cmd_impersonate
from cli.commands.agents.tests.lease_support import ActiveSession
from cli.commands.agents.tests.lease_support import active as active
from cli.commands.agents.tests.lease_support import idle_agent as idle_agent
from cli.commands.agents.tests.loopback_hosts import CodexHome
from cli.commands.agents.tests.loopback_hosts import codex_home as codex_home
from cli.commands.cluster.tests.gateway_server import Reply, serving
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime, Terminal
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime
from cli.parsers import build_parser

THREAD = "b9d32d0d-bd27-40fc-83e8-692769b21523"
NO_ENV = ImpersonationEnv(
    relay_token=None, dsh_relay_stub=None, antigravity_conversation=None, codex_home=None
)
CSRF = "recorded-csrf"


def _args(*args: str, command_runtime: CommandRuntime) -> Namespace:
    parsed = build_parser().parse_args(["impersonate", *args])
    parsed.runtime = command_runtime
    return parsed


def _run(
    *args: str,
    runtime: CommandRuntime,
    database: OperatorDatabaseFactory,
    env: ImpersonationEnv = NO_ENV,
) -> int:
    return cmd_impersonate(
        _args(*args, command_runtime=runtime), database_factory=database, env=env
    )


def _stdin(runtime: CommandRuntime, text: str) -> CommandRuntime:
    terminal = Terminal(io.StringIO(text), runtime.terminal.stdout, runtime.terminal.stderr)
    return replace(runtime, terminal=terminal)


def _request_args(agent_id: int, *tail: str) -> list[str]:
    return [
        "request",
        "--name",
        "Fix login",
        "--agent",
        str(agent_id),
        "--as",
        "Codex: task1",
        "--ttl",
        "600",
        "--batch-window",
        "0",
        *tail,
    ]


def _lease_rows(db_conn: psycopg.Connection, agent_id: int) -> list[dict[str, object]]:
    cursor = db_conn.execute(
        "SELECT name,executor_name,ttl_seconds,reason,relay_provider,relay_thread_id,"
        "relay_codex_remote,relay_batch_window_seconds,process_metadata "
        "FROM agent_impersonations WHERE agent_id=%s ORDER BY session_id",
        (agent_id,),
    )
    columns = [column.name for column in cursor.description or ()]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _lease_id(db_conn: psycopg.Connection, agent_id: int) -> str:
    row = db_conn.execute(
        "SELECT id FROM agent_impersonations WHERE agent_id=%s", (agent_id,)
    ).fetchone()
    assert row is not None
    return str(row[0])


def test_timeline_command_dispatches_to_the_timeline_reader(
    capsys: pytest.CaptureFixture[str], *, command_runtime: CommandRuntime
) -> None:
    args = build_parser().parse_args(
        ["agents", "timeline", "405", "--limit", "100", "--before", "12.0"]
    )
    args.runtime = command_runtime
    timeline = {"items": [], "msg_count": 0, "has_more": False}
    with serving({("GET", "/api/agents/405/timeline"): Reply(body=timeline)}) as gateway:
        command_runtime.config.set_field("gateway_url", gateway.url)
        command_runtime.config.set_field("cluster_secret", "test-cluster-secret")
        assert args.func(args) == 0
    assert [request.target for request in gateway.requests] == [
        "/api/agents/405/timeline?limit=100&before=12.0"
    ]
    assert json.loads(capsys.readouterr().out) == timeline


def test_request_records_the_shared_app_server_endpoint(
    idle_agent: tuple[int, RuntimeIncarnation],
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    agent_id, _ = idle_agent
    endpoint = "unix:///home/u/.meiva-lc/run/codex-app-server.0123456789ab-01234567.sock"
    code = _run(
        *_request_args(
            agent_id,
            "--provider",
            "codex",
            "--thread-id",
            "thread-1",
            "--codex-remote",
            endpoint,
        ),
        runtime=command_runtime,
        database=operator_database,
    )
    assert code == 0
    [row] = _lease_rows(db_conn, agent_id)
    assert (row["relay_provider"], row["relay_thread_id"]) == ("codex", "thread-1")
    assert row["relay_codex_remote"] == endpoint
    assert "pass --codex-remote" in capsys.readouterr().err


def test_request_uses_external_identity_without_delivering_a_credential(
    idle_agent: tuple[int, RuntimeIncarnation],
    codex_home: CodexHome,
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    agent_id, _ = idle_agent
    codex_home.socket.touch()
    code = _run(
        *_request_args(agent_id, "--provider", "codex", "--thread-id", "thread-1"),
        runtime=command_runtime,
        database=operator_database,
        env=replace(NO_ENV, codex_home=codex_home.home),
    )
    assert code == 0
    [row] = _lease_rows(db_conn, agent_id)
    assert row["name"] == "Fix login"
    assert row["executor_name"] == "Codex: task1"
    assert row["ttl_seconds"] == 600
    assert (row["relay_provider"], row["relay_thread_id"]) == ("codex", "thread-1")
    assert row["relay_codex_remote"] == f"unix://{codex_home.socket}"
    assert row["relay_batch_window_seconds"] == 0
    metadata = row["process_metadata"]
    assert isinstance(metadata, dict)
    assert metadata["pid"] > 0
    assert metadata["invoked_python"] == sys.executable
    output = capsys.readouterr()
    assert "token" not in json.loads(output.out)
    assert "starts the codex relay automatically" in output.err


def test_request_without_steer_endpoint_does_not_acquire_a_lease(
    idle_agent: tuple[int, RuntimeIncarnation],
    codex_home: CodexHome,
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    agent_id, _ = idle_agent
    code = _run(
        *_request_args(agent_id, "--provider", "codex", "--thread-id", "thread-1"),
        runtime=command_runtime,
        database=operator_database,
        env=replace(NO_ENV, codex_home=codex_home.home),
    )
    assert code == 1
    assert "Steer" in capsys.readouterr().err
    assert _lease_rows(db_conn, agent_id) == []


def test_claude_request_reports_the_relay_handoff(
    idle_agent: tuple[int, RuntimeIncarnation],
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
    machine_identity: MachineIdentity,
) -> None:
    agent_id, _ = idle_agent
    code = _run(
        *_request_args(agent_id, "--provider", "claude"),
        runtime=command_runtime,
        database=operator_database,
    )
    assert code == 0
    output = capsys.readouterr()
    response = json.loads(output.out)
    token = response["relay_token"]
    assert "MEIVA_IMPERSONATION_RELAY_TOKEN" in output.err
    assert token not in output.err
    [row] = _lease_rows(db_conn, agent_id)
    assert (row["relay_provider"], row["relay_thread_id"], row["relay_codex_remote"]) == (
        "claude",
        None,
        None,
    )
    # meiva:note
    # The credential printed is the one the lease minted.
    snapshot = leases.relay_get(
        database,
        event_bus,
        _lease_id(db_conn, agent_id),
        token,
        local_machine=machine_identity.name(),
    )
    assert snapshot["status"] == ImpersonationStatus.REQUESTED


def test_a_dsh_request_without_the_plugin_is_refused_before_any_lease(
    idle_agent: tuple[int, RuntimeIncarnation],
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    agent_id, _ = idle_agent
    code = _run(
        *_request_args(agent_id, "--provider", "dsh"),
        runtime=command_runtime,
        database=operator_database,
    )
    assert code == 1
    assert "DSH_MEIVA_RELAY_STUB is unset" in capsys.readouterr().err
    assert _lease_rows(db_conn, agent_id) == []


def test_dsh_request_writes_the_plugin_stub_and_never_prints_the_credential(
    idle_agent: tuple[int, RuntimeIncarnation],
    tmp_path: Path,
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
    machine_identity: MachineIdentity,
) -> None:
    agent_id, _ = idle_agent
    stub = tmp_path / "session-1.env"
    env = replace(NO_ENV, dsh_relay_stub=str(stub))
    code = _run(
        *_request_args(agent_id, "--provider", "dsh"),
        runtime=command_runtime,
        database=operator_database,
        env=env,
    )
    assert code == 0
    output = capsys.readouterr()
    response = json.loads(output.out)
    assert "relay_token" not in response
    assert "credential stub" in output.err
    text = stub.read_text()
    match = re.fullmatch(
        rf"SID={response['session_id']}\nAGENT={agent_id}\n"
        r"MEIVA_IMPERSONATION_RELAY_TOKEN=(?P<token>\S+)\n"
        rf"MEIVA_IMPERSONATION_RELAY_PY={re.escape(sys.executable)}\n",
        text,
    )
    assert match is not None, text
    token = match["token"]
    assert token not in output.out + output.err
    assert stat.S_IMODE(stub.stat().st_mode) == 0o600
    # meiva:note
    # The stub holds the credential the lease minted.
    snapshot = leases.relay_get(
        database,
        event_bus,
        _lease_id(db_conn, agent_id),
        token,
        local_machine=machine_identity.name(),
    )
    assert snapshot["status"] == ImpersonationStatus.REQUESTED


@pytest.mark.parametrize("from_environment", [False, True])
def test_an_antigravity_request_records_its_endpoint_and_names_the_conversation(
    from_environment: bool,
    idle_agent: tuple[int, RuntimeIncarnation],
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    agent_id, _ = idle_agent
    home = command_runtime.home
    server = AntigravityServer(address="127.0.0.1:59901", csrf_token=CSRF)
    record = write_antigravity_endpoint(server, thread_id=THREAD, home=home)
    tail = [] if from_environment else ["--thread-id", THREAD.upper()]
    env = replace(NO_ENV, antigravity_conversation=THREAD if from_environment else None)
    code = _run(
        *_request_args(agent_id, "--provider", "antigravity", *tail),
        runtime=command_runtime,
        database=operator_database,
        env=env,
    )
    assert code == 0
    [row] = _lease_rows(db_conn, agent_id)
    assert (row["relay_provider"], row["relay_thread_id"]) == ("antigravity", THREAD)
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    assert json.loads(record.read_text())["conversation_id"] == THREAD
    assert "starts the antigravity relay automatically" in capsys.readouterr().err


def test_an_antigravity_request_without_a_conversation_is_refused_before_any_lease(
    idle_agent: tuple[int, RuntimeIncarnation],
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    agent_id, _ = idle_agent
    code = _run(
        *_request_args(agent_id, "--provider", "antigravity"),
        runtime=command_runtime,
        database=operator_database,
    )
    assert code == 1
    assert "--thread-id or ANTIGRAVITY_CONVERSATION_ID" in capsys.readouterr().err
    assert _lease_rows(db_conn, agent_id) == []


def test_a_request_for_an_agent_that_is_not_on_this_machine_is_refused(
    idle_agent: tuple[int, RuntimeIncarnation],
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    agent_id, _ = idle_agent
    command_runtime.machine.set(name="some-other-machine")
    code = _run(
        *_request_args(agent_id, "--provider", "claude"),
        runtime=command_runtime,
        database=operator_database,
    )
    assert code == 1
    assert "some-other-machine" in capsys.readouterr().err
    assert _lease_rows(db_conn, agent_id) == []


def test_ack_uses_explicit_processed_ids_only(
    active: ActiveSession,
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    lease, _, mid = active
    code = _run(
        "ack",
        str(lease["session_id"]),
        str(mid),
        "--agent",
        str(lease["agent_id"]),
        runtime=command_runtime,
        database=operator_database,
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"acknowledged": [mid]}
    assert db_conn.execute(
        "SELECT status FROM inbound_messages WHERE id=%s", (mid,)
    ).fetchone() == ("done",)


def test_status_shows_the_public_session(
    active: ActiveSession,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    lease, _, _ = active
    code = _run(
        "status",
        str(lease["session_id"]),
        "--agent",
        str(lease["agent_id"]),
        runtime=command_runtime,
        database=operator_database,
    )
    assert code == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["status"] == "active"
    assert shown["session_id"] == lease["session_id"]
    assert "relay_token" not in shown and "relay_token_hash" not in shown


def test_classified_attestation_refusal_fails_without_leaking_state(
    active: ActiveSession,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    lease, _, _ = active
    command_runtime.machine.set(name="some-other-machine")
    code = _run(
        "status",
        str(lease["session_id"]),
        "--agent",
        str(lease["agent_id"]),
        runtime=command_runtime,
        database=operator_database,
    )
    output = capsys.readouterr()
    assert code == 1
    assert output.out == ""
    assert "some-other-machine" in output.err


def test_an_unknown_session_is_a_one_line_refusal_not_a_traceback(
    active: ActiveSession,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    lease, _, _ = active
    code = _run(
        "status",
        "999",
        "--agent",
        str(lease["agent_id"]),
        runtime=command_runtime,
        database=operator_database,
    )
    output = capsys.readouterr()
    assert code == 1
    assert output.out == ""
    assert output.err.startswith("impersonation: ")
    assert "Traceback" not in output.err


@pytest.mark.parametrize("via_stdin", [False, True])
def test_release_preserves_summary(
    via_stdin: bool,
    active: ActiveSession,
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    lease, _, _ = active
    summary = "Completed X.\nNext Y."
    runtime = _stdin(command_runtime, summary) if via_stdin else command_runtime
    code = _run(
        "release",
        str(lease["session_id"]),
        "--agent",
        str(lease["agent_id"]),
        "--summary",
        "-" if via_stdin else summary,
        runtime=runtime,
        database=operator_database,
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "released"
    row = db_conn.execute(
        "SELECT status, summary FROM agent_impersonations WHERE id=%s", (lease["id"],)
    ).fetchone()
    assert row == ("released", summary)


def test_renew_extends_the_lease_to_the_new_lifetime(
    active: ActiveSession,
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    lease, _, _ = active
    code = _run(
        "renew",
        str(lease["session_id"]),
        "--agent",
        str(lease["agent_id"]),
        "--ttl",
        "900",
        runtime=command_runtime,
        database=operator_database,
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["ttl_seconds"] == 900
    assert db_conn.execute(
        "SELECT ttl_seconds FROM agent_impersonations WHERE id=%s", (lease["id"],)
    ).fetchone() == (900,)


def test_inbox_lists_the_pending_rows_at_once_when_not_asked_to_wait(
    active: ActiveSession,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    lease, _, mid = active
    code = _run(
        "inbox",
        str(lease["session_id"]),
        "--agent",
        str(lease["agent_id"]),
        runtime=command_runtime,
        database=operator_database,
    )
    assert code == 0
    assert [row["id"] for row in json.loads(capsys.readouterr().out)] == [mid]


def test_inbox_waits_for_new_input_and_returns_empty_at_the_deadline(
    active: ActiveSession,
    db_conn: psycopg.Connection,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    lease, _, mid = active
    db_conn.execute("UPDATE inbound_messages SET status='done' WHERE id=%s", (mid,))
    db_conn.commit()
    code = _run(
        "inbox",
        str(lease["session_id"]),
        "--agent",
        str(lease["agent_id"]),
        "--wait",
        "0.5",
        runtime=command_runtime,
        database=operator_database,
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out) == []


def test_say_records_the_controllers_reply_under_its_retry_key(
    active: ActiveSession,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    lease, _, _ = active
    base = ["say", str(lease["session_id"]), "--agent", str(lease["agent_id"])]
    code = _run(*base, "hello", "--key", "k1", runtime=command_runtime, database=operator_database)
    assert code == 0
    first = json.loads(capsys.readouterr().out)
    again = _run(*base, "hello", "--key", "k1", runtime=command_runtime, database=operator_database)
    assert again == 0
    assert json.loads(capsys.readouterr().out) == first


def test_list_pages_the_agents_permanent_history(
    active: ActiveSession,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    lease, _, _ = active
    code = _run(
        "list",
        "--agent",
        str(lease["agent_id"]),
        runtime=command_runtime,
        database=operator_database,
    )
    assert code == 0
    assert [row["session_id"] for row in json.loads(capsys.readouterr().out)] == [
        lease["session_id"]
    ]


def test_send_delivers_as_the_borrowed_agent(
    active: ActiveSession,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
    *,
    command_runtime: CommandRuntime,
) -> None:
    lease, _, _ = active
    with serving(
        {("POST", "/api/agents/42/messages"): Reply(body={"status": "enqueued"})}
    ) as gateway:
        command_runtime.config.set_field("gateway_url", gateway.url)
        command_runtime.config.set_field("cluster_secret", "test-cluster-secret")
        code = _run(
            "send",
            str(lease["session_id"]),
            "--agent",
            str(lease["agent_id"]),
            "--to",
            "42",
            "--content",
            "hi",
            runtime=command_runtime,
            database=operator_database,
        )
    assert code == 0
    [request] = gateway.requests
    assert request.body == {"content": "hi", "source": f"agent:{lease['agent_id']}"}
    assert request.headers["authorization"] == "Bearer test-cluster-secret"
    assert json.loads(capsys.readouterr().out) == {
        "status": "enqueued",
        "to": 42,
        "source": f"agent:{lease['agent_id']}",
    }


def test_send_reads_a_dash_content_from_the_invocations_stdin(
    active: ActiveSession,
    operator_database: OperatorDatabaseFactory,
    *,
    command_runtime: CommandRuntime,
) -> None:
    lease, _, _ = active
    with serving(
        {("POST", "/api/agents/42/messages"): Reply(body={"status": "enqueued"})}
    ) as gateway:
        command_runtime.config.set_field("gateway_url", gateway.url)
        command_runtime.config.set_field("cluster_secret", "test-cluster-secret")
        code = _run(
            "send",
            str(lease["session_id"]),
            "--agent",
            str(lease["agent_id"]),
            "--to",
            "42",
            "--content",
            "-",
            runtime=_stdin(command_runtime, "from stdin\nsecond line"),
            database=operator_database,
        )
    assert code == 0
    assert gateway.requests[0].body["content"] == "from stdin\nsecond line"
