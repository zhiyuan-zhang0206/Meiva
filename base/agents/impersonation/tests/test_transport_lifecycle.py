import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import psycopg
import pytest

from base.agents import impersonation as leases
from base.agents.impersonation import host_transport, relay, terminal_notices
from base.agents.impersonation.tests.impersonation_support import recorded_tree
from base.api_contracts.caller_identity import CallerIdentity
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database, create_agent
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation


@pytest.fixture
def active(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
) -> tuple[dict[str, Any], RuntimeIncarnation]:
    local_machine = machine_identity.name()
    aid = create_agent(db_conn)
    owner = RuntimeIncarnation(aid, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner) "
        "VALUES(%s,'idling',%s,%s,%s)",
        (aid, local_machine, owner.generation, owner.owner),
    )
    db_conn.commit()
    session = leases.request(
        database,
        event_bus,
        aid,
        caller=CallerIdentity(kind="external_agent", subject="codex", instance="test"),
        ttl_seconds=3600,
        reason="Lifecycle contract test",
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
        relay_codex_remote="unix:///tmp/recorded-codex.sock",
        process_metadata=recorded_tree(),
        authority=config_authority,
        local_machine=local_machine,
        name="Test name",
        executor_name="Test executor_name",
    )
    leases.prepare(
        database,
        event_bus,
        session["id"],
        aid,
        owner,
        "Continue this task",
        local_machine=local_machine,
    )
    leases.activate(database, event_bus, session["id"], owner)
    return session, owner


def end(
    database: Database, event_bus: EventBus, session: dict[str, Any], *, local_machine: str
) -> None:
    leases.release(
        database,
        event_bus,
        session["id"],
        "Saved unfinished task",
        local_machine=local_machine,
    )


def test_generation_claim_is_single_winner_and_stale_token_loses_authority(
    active: tuple[dict[str, Any], RuntimeIncarnation],
    database: Database,
    event_bus: EventBus,
    *,
    machine_identity: MachineIdentity,
) -> None:
    session, owner = active
    tokens = ["first-private-token", "second-private-token"]

    def claim(token: str) -> dict[str, Any] | None:
        return relay.provision_relay(database, session["id"], owner, token)

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(claim, tokens))
    assert sum(value is not None for value in outcomes) == 1
    winner = tokens[next(i for i, value in enumerate(outcomes) if value is not None)]
    other = tokens[1 - tokens.index(winner)]
    with pytest.raises(leases.ImpersonationError, match="Invalid relay token"):
        leases.relay_heartbeat(
            database, session["id"], other, local_machine=machine_identity.name()
        )
    latest = leases.relay_get(
        database, event_bus, session["id"], winner, local_machine=machine_identity.name()
    )
    assert latest["relay_generation"] == 1
    assert latest["expires_at"] <= session["expires_at"] + timedelta(seconds=1)
    assert relay.provision_relay(database, session["id"], owner, "loser") is None
    assert (
        leases.relay_get(
            database, event_bus, session["id"], winner, local_machine=machine_identity.name()
        )["relay_generation"]
        == 1
    )


def test_ended_notice_is_submitted_once_to_the_recorded_destination(
    active: tuple[dict[str, Any], RuntimeIncarnation],
    database: Database,
    event_bus: EventBus,
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    session, _ = active
    end(database, event_bus, session, local_machine=machine_identity.name())
    submitted = Mock(return_value=None)
    monkeypatch.setattr(host_transport, "live_submit", submitted)
    assert terminal_notices.send_ended_notice(database, session["id"]) is None
    submitted.assert_called_once()
    assert submitted.call_args.args[0] == session["relay_thread_id"]
    assert submitted.call_args.kwargs["endpoint"] == session["relay_codex_remote"]
    text = submitted.call_args.args[1]
    assert session["id"] in text and "does not end or cancel any newer" in text


def test_ended_notice_failure_is_reported_and_leaves_no_retry_state(
    active: tuple[dict[str, Any], RuntimeIncarnation],
    database: Database,
    event_bus: EventBus,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    session, _ = active
    end(database, event_bus, session, local_machine=machine_identity.name())
    submitted = Mock(return_value="TimeoutError: ambiguous acceptance")
    monkeypatch.setattr(host_transport, "live_submit", submitted)
    assert (
        terminal_notices.send_ended_notice(database, session["id"])
        == "TimeoutError: ambiguous acceptance"
    )
    submitted.assert_called_once()
    assert db_conn.execute(
        "SELECT terminal_notice_attempts,terminal_notice_accepted_at,terminal_notice_error "
        "FROM agent_impersonations WHERE id=%s",
        (session["id"],),
    ).fetchone() == (0, None, None)


def test_controller_session_destination_gets_no_host_submission(
    active: tuple[dict[str, Any], RuntimeIncarnation],
    database: Database,
    event_bus: EventBus,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    session, _ = active
    db_conn.execute(
        "UPDATE agent_impersonations SET relay_provider='claude',relay_thread_id=NULL,relay_codex_remote=NULL WHERE id=%s",
        (session["id"],),
    )
    db_conn.commit()
    end(database, event_bus, session, local_machine=machine_identity.name())
    monkeypatch.setattr(
        host_transport,
        "live_submit",
        Mock(side_effect=AssertionError("Unsupported destination must not submit")),
    )
    assert terminal_notices.send_ended_notice(database, session["id"]) is None


def test_old_notice_keeps_destination_after_replacement_takeover(
    active: tuple[dict[str, Any], RuntimeIncarnation],
    database: Database,
    event_bus: EventBus,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
    config_authority: ConfigAuthority,
) -> None:
    session, owner = active
    end(database, event_bus, session, local_machine=machine_identity.name())
    db_conn.execute(
        "UPDATE agent_impersonations SET handoff_applied_at=clock_timestamp() WHERE id=%s",
        (session["id"],),
    )
    db_conn.commit()
    replacement = leases.request(
        database,
        event_bus,
        owner.agent_id,
        caller=CallerIdentity(kind="external_agent", subject="codex", instance="replacement"),
        ttl_seconds=3600,
        reason="Replacement task",
        relay_provider="codex",
        relay_thread_id=str(uuid4()),
        relay_codex_remote="unix:///tmp/replacement.sock",
        process_metadata=recorded_tree(),
        authority=config_authority,
        local_machine=machine_identity.name(),
        name="Test name",
        executor_name="Test executor_name",
    )
    leases.prepare(
        database,
        event_bus,
        replacement["id"],
        owner.agent_id,
        owner,
        "Replacement task",
        local_machine=machine_identity.name(),
    )
    leases.activate(database, event_bus, replacement["id"], owner)
    db_conn.execute(
        "UPDATE agent_impersonations SET relay_thread_id=%s,relay_codex_remote=%s WHERE id=%s",
        (replacement["relay_thread_id"], replacement["relay_codex_remote"], session["id"]),
    )
    db_conn.commit()
    submitted = Mock(return_value=None)
    monkeypatch.setattr(host_transport, "live_submit", submitted)
    assert terminal_notices.send_ended_notice(database, session["id"]) is None
    assert submitted.call_args.args[0] == session["relay_thread_id"]
    assert submitted.call_args.kwargs["endpoint"] == session["relay_codex_remote"]
    assert session["id"] in submitted.call_args.args[1]
    assert replacement["id"] not in submitted.call_args.args[1]
    assert (
        leases.require_active(
            database,
            replacement["id"],
            local_machine=machine_identity.name(),
        )["status"]
        == "active"
    )


def test_terminated_agent_notice_names_the_termination(
    active: tuple[dict[str, Any], RuntimeIncarnation],
    database: Database,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    session, owner = active
    db_conn.execute("UPDATE agents_meta SET status='terminated' WHERE id=%s", (owner.agent_id,))
    db_conn.commit()
    submitted = Mock(return_value=None)
    monkeypatch.setattr(host_transport, "live_submit", submitted)
    assert terminal_notices.send_ended_notice(database, session["id"]) is None
    assert "terminated: agent was terminated" in submitted.call_args.args[1]


def test_already_terminal_insert_is_not_historically_backfilled(
    active: tuple[dict[str, Any], RuntimeIncarnation],
    database: Database,
    event_bus: EventBus,
    db_conn: psycopg.Connection,
    *,
    machine_identity: MachineIdentity,
) -> None:
    session, owner = active
    end(database, event_bus, session, local_machine=machine_identity.name())
    legacy_id = uuid4()
    db_conn.execute(
        "INSERT INTO agent_impersonations(id,agent_id,source,machine,status,ttl_seconds,expires_at,ended_at,handoff_applied_at) "
        "VALUES(%s,%s,'external_agent:legacy',%s,'expired',300,clock_timestamp(),clock_timestamp(),clock_timestamp())",
        (legacy_id, owner.agent_id, machine_identity.name()),
    )
    db_conn.commit()
    assert db_conn.execute(
        "SELECT terminal_notice_pending_at FROM agent_impersonations WHERE id=%s", (legacy_id,)
    ).fetchone() == (None,)
    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        db_conn.execute(
            "UPDATE agent_impersonations SET terminal_notice_snapshot='{}'::jsonb WHERE id=%s",
            (session["id"],),
        )
    db_conn.rollback()


def test_ended_antigravity_notice_uses_the_endpoint_the_request_recorded(
    active: tuple[dict[str, Any], RuntimeIncarnation],
    database: Database,
    event_bus: EventBus,
    db_conn: psycopg.Connection,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentity,
) -> None:
    from functools import partial
    from http.server import BaseHTTPRequestHandler, HTTPServer

    session, _ = active
    thread_id = str(uuid4())
    db_conn.execute(
        "UPDATE agent_impersonations SET relay_provider='antigravity',relay_thread_id=%s,"
        "relay_codex_remote=NULL WHERE id=%s",
        (thread_id, session["id"]),
    )
    db_conn.commit()
    received: list[tuple[str, str | None, dict[str, Any]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            received.append((self.path, self.headers.get("x-codeium-csrf-token"), body))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    csrf = "recorded-csrf"
    recorded = host_transport.AntigravityServer(address=f"127.0.0.1:{port}", csrf_token=csrf)
    host_transport.write_antigravity_endpoint(recorded, thread_id=thread_id, home=tmp_path)
    end(database, event_bus, session, local_machine=machine_identity.name())
    try:
        reason = terminal_notices.send_ended_notice(
            database,
            session["id"],
            resolve_antigravity=partial(
                host_transport.resolve_antigravity_endpoint, home=tmp_path, environment={}
            ),
        )
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
    assert reason is None
    assert len(received) == 1
    path, header_value, body = received[0]
    assert path == "/exa.language_server_pb.LanguageServerService/SendAgentMessage"
    assert header_value == csrf
    assert body["recipient"] == thread_id
    assert session["id"] in body["content"]


def test_ended_antigravity_notice_without_a_record_names_the_record_it_looked_for(
    active: tuple[dict[str, Any], RuntimeIncarnation],
    database: Database,
    event_bus: EventBus,
    db_conn: psycopg.Connection,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentity,
) -> None:
    session, _ = active
    thread_id = str(uuid4())
    db_conn.execute(
        "UPDATE agent_impersonations SET relay_provider='antigravity',relay_thread_id=%s,"
        "relay_codex_remote=NULL WHERE id=%s",
        (thread_id, session["id"]),
    )
    db_conn.commit()
    path = host_transport.antigravity_endpoint_path(thread_id, home=tmp_path)

    def missing(_thread_id: str) -> "host_transport.AntigravityServer":
        raise RuntimeError(f"No Antigravity endpoint record exists at {path}")

    end(database, event_bus, session, local_machine=machine_identity.name())
    reason = terminal_notices.send_ended_notice(
        database, session["id"], resolve_antigravity=missing
    )
    assert reason is not None
    assert str(path) in reason
