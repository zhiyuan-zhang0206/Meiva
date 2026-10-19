"""Live delivery into a running codex app server: the relay's fast path.

The fake app server speaks codex's own transport (JSON-RPC over a websocket on
a unix socket). The contract pinned here: initialize then initialized, one
``turn/start``; a result means delivered, while a refusal, a timeout, a dead
endpoint or an unsupported endpoint shape returns a reason the relay reports
as failure. The relay-level Steer-only wiring lives in
``test_impersonation_bridge.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from websockets.sync.server import Server, ServerConnection, unix_serve

from base.agents.impersonation import host_transport as codex_app_server
from base.agents.impersonation.terminal_notices import send_ended_notice
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database, create_agent

THREAD_ID = UUID("b9d32d0d-bd27-40fc-83e8-692769b21523")


def _endpoint(path: Path) -> str:
    return f"unix://{path}"


@pytest.fixture
def short_socket() -> Iterator[Path]:
    """A bind path short enough for macOS's 104-char AF_UNIX limit.

    pytest's ``tmp_path`` is too deep on macOS (meiva/mcps/tests/test_daemon.py precedent:
    the limit bites during bind, not connect); a short /tmp directory keeps
    every bind path legal on both platforms. Self-managed and removed on
    teardown.
    """
    # Short bind path: macOS AF_UNIX limit is 104 chars, CI Linux 107.
    base = Path(tempfile.mkdtemp(prefix="cxas-", dir="/tmp"))
    try:
        yield base / "codex.sock"
    finally:
        shutil.rmtree(base, ignore_errors=True)


class FakeAppServer:
    """Minimal codex app server: JSON-RPC over a unix-socket websocket.

    ``interleave`` messages are sent ahead of the turn/start reply — the
    notifications and server-initiated requests the client must skip;
    ``hold_reply`` drops the reply so only the interleave ever arrives.
    """

    def __init__(
        self,
        *,
        silent: bool = False,
        refuse_turn: bool = False,
        interleave: Sequence[dict[str, Any]] = (),
        hold_reply: bool = False,
    ) -> None:
        self.silent = silent
        self.refuse_turn = refuse_turn
        self.interleave = list(interleave)
        self.hold_reply = hold_reply
        self.received: list[dict[str, Any]] = []
        self._server: Server | None = None
        self._thread: threading.Thread | None = None

    def start(self, path: Path) -> None:
        self._server = unix_serve(
            self._handle, path=str(path), open_timeout=5, close_timeout=1, compression=None
        )
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _handle(self, conn: ServerConnection) -> None:
        if self.silent:
            for _ in conn:  # accepted, never answered: drain until the client gives up
                pass
            return
        for raw in conn:
            message = json.loads(raw)
            self.received.append(message)
            method = message.get("method")
            if method == "initialize":
                conn.send(json.dumps({"id": message["id"], "result": {"userAgent": "fake"}}))
            elif method == "turn/start":
                for extra in self.interleave:
                    conn.send(json.dumps(extra))
                if self.hold_reply:
                    continue
                if self.refuse_turn:
                    conn.send(
                        json.dumps(
                            {
                                "id": message["id"],
                                "error": {
                                    "code": -32603,
                                    "message": (
                                        "failed to submit turn input: "
                                        "ActiveTurnNotSteerable { turn_kind: Review }"
                                    ),
                                },
                            }
                        )
                    )
                else:
                    conn.send(
                        json.dumps({"id": message["id"], "result": {"turn": {"id": "turn-1"}}})
                    )


def test_live_submit_delivers_over_the_control_socket(short_socket: Path) -> None:
    server = FakeAppServer()
    server.start(short_socket)
    try:
        reason = codex_app_server.live_submit(
            str(THREAD_ID), "hello host", endpoint=_endpoint(short_socket), timeout=3.0
        )
    finally:
        server.stop()
    assert reason is None
    assert [m.get("method") for m in server.received] == [
        "initialize",
        "initialized",
        "turn/start",
    ]
    assert server.received[0]["params"]["clientInfo"]["name"] == "meiva-impersonation-relay"
    assert server.received[-1]["params"] == {
        "threadId": str(THREAD_ID),
        "input": [{"type": "text", "text": "hello host"}],
    }


def test_live_submit_reports_a_refused_turn(short_socket: Path) -> None:
    server = FakeAppServer(refuse_turn=True)
    server.start(short_socket)
    try:
        reason = codex_app_server.live_submit(
            str(THREAD_ID), "parked", endpoint=_endpoint(short_socket), timeout=3.0
        )
    finally:
        server.stop()
    assert reason is not None
    assert "turn/start refused" in reason
    assert "ActiveTurnNotSteerable" in reason


def test_live_submit_skips_interleaved_server_traffic(short_socket: Path) -> None:
    spoof = {"id": codex_app_server.TURN_REQUEST_ID, "method": "item/requestApproval"}
    server = FakeAppServer(
        interleave=[
            {"method": "remoteControl/status/changed", "params": {"status": "disabled"}},
            spoof,
        ]
    )
    server.start(short_socket)
    try:
        reason = codex_app_server.live_submit(
            str(THREAD_ID), "delivered anyway", endpoint=_endpoint(short_socket), timeout=3.0
        )
    finally:
        server.stop()
    assert reason is None


def test_live_submit_does_not_mistake_a_server_request_for_its_reply(
    short_socket: Path,
) -> None:
    """An id collision without result/error is not a reply: the client times out."""
    spoof = {"id": codex_app_server.TURN_REQUEST_ID, "method": "item/requestApproval"}
    server = FakeAppServer(interleave=[spoof], hold_reply=True)
    server.start(short_socket)
    started = time.monotonic()
    try:
        reason = codex_app_server.live_submit(
            str(THREAD_ID), "unanswered", endpoint=_endpoint(short_socket), timeout=0.3
        )
    finally:
        server.stop()
    assert reason is not None
    assert time.monotonic() - started < 5.0


def test_live_submit_bounds_a_silent_server(short_socket: Path) -> None:
    server = FakeAppServer(silent=True)
    server.start(short_socket)
    started = time.monotonic()
    try:
        reason = codex_app_server.live_submit(
            str(THREAD_ID), "unheard", endpoint=_endpoint(short_socket), timeout=0.3
        )
    finally:
        server.stop()
    assert reason is not None
    assert time.monotonic() - started < 5.0


def test_live_submit_reports_an_unreachable_endpoint(tmp_path: Path) -> None:
    reason = codex_app_server.live_submit(
        str(THREAD_ID), "lost", endpoint=_endpoint(tmp_path / "missing.sock"), timeout=1.0
    )
    assert reason is not None


def test_live_submit_reports_an_unsupported_endpoint() -> None:
    reason = codex_app_server.live_submit(
        str(THREAD_ID), "nope", endpoint="tcp://host:1", timeout=1.0
    )
    assert reason is not None
    assert "unsupported" in reason


def test_default_control_endpoint_follows_codex_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert codex_app_server.default_control_endpoint() is None
    sock = tmp_path / "app-server-control" / "app-server-control.sock"
    sock.parent.mkdir(parents=True)
    sock.touch()
    assert codex_app_server.default_control_endpoint() == f"unix://{sock}"


def test_live_submit_treats_a_bare_unix_endpoint_as_the_default_socket(
    short_socket: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare ``unix://`` resolves through ``default_control_socket()``.

    Its CODEX_HOME mapping stays covered by
    ``test_default_control_endpoint_follows_codex_home``; the bind goes through
    the short-socket fixture like every other test that starts a fake server.
    """

    def short_default_socket() -> Path:
        return short_socket

    monkeypatch.setattr(codex_app_server, "default_control_socket", short_default_socket)
    server = FakeAppServer()
    server.start(short_socket)
    try:
        reason = codex_app_server.live_submit(str(THREAD_ID), "hi", endpoint="unix://", timeout=3.0)
    finally:
        server.stop()
    assert reason is None


def test_ended_lease_reaches_actual_codex_host_without_a_relay(
    short_socket: Path,
    database: Database,
    db_conn: psycopg.Connection,
    *,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    from uuid import uuid4

    aid, lease_id = create_agent(db_conn), uuid4()
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine) VALUES(%s,'terminated',%s)",
        (aid, local_machine),
    )
    db_conn.execute(
        "INSERT INTO agent_impersonations(id,agent_id,source,machine,status,ttl_seconds,expires_at,relay_provider,relay_thread_id,relay_codex_remote) "
        "VALUES(%s,%s,'external_agent:test',%s,'active',300,clock_timestamp()+interval '5 minutes','codex',%s,%s)",
        (lease_id, aid, local_machine, str(THREAD_ID), _endpoint(short_socket)),
    )
    db_conn.execute(
        "UPDATE agent_impersonations SET status='expired',ended_at=clock_timestamp(),rejection_reason='confirmed executor death' WHERE id=%s",
        (lease_id,),
    )
    db_conn.commit()
    server = FakeAppServer()
    server.start(short_socket)
    try:
        assert send_ended_notice(database, str(lease_id)) is None
    finally:
        server.stop()
    payload = server.received[-1]["params"]
    assert payload["threadId"] == str(THREAD_ID)
    assert str(lease_id) in payload["input"][0]["text"]
    assert "confirmed executor death" in payload["input"][0]["text"]


def test_antigravity_live_submit_posts_send_agent_message_with_csrf_token() -> None:
    from http.server import BaseHTTPRequestHandler, HTTPServer

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
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    expected_csrf = "test-csrf-header"
    try:
        server = codex_app_server.AntigravityServer(
            address=f"127.0.0.1:{port}", csrf_token=expected_csrf
        )
        reason = codex_app_server.antigravity_live_submit(
            str(THREAD_ID), "hello antigravity", server=server, timeout=3.0
        )
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()

    assert reason is None
    assert len(received) == 1
    path, header_value, body = received[0]
    assert path == "/exa.language_server_pb.LanguageServerService/SendAgentMessage"
    assert header_value == expected_csrf
    assert body == {"recipient": str(THREAD_ID), "content": "hello antigravity"}


# meiva:note
# A loopback Connect endpoint that records the CSRF header it was called with, so a
# test can assert which credential reached the server rather than which one a
# function was handed.
def _antigravity_http_stub() -> tuple[Any, list[str | None], threading.Thread]:
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            seen.append(self.headers.get("x-codeium-csrf-token"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, seen, thread


def _no_scan(**_kwargs: object) -> codex_app_server.AntigravityServer:
    raise AssertionError("a resolved environment or a record needs no process scan")


def test_antigravity_endpoint_record_is_owner_only_and_round_trips(tmp_path: Path) -> None:
    csrf = "local-csrf"
    server = codex_app_server.AntigravityServer(address="127.0.0.1:59901", csrf_token=csrf)
    path = codex_app_server.write_antigravity_endpoint(
        server, thread_id=str(THREAD_ID), home=tmp_path
    )
    assert path == codex_app_server.antigravity_endpoint_path(str(THREAD_ID), home=tmp_path)
    assert path.name == f"{THREAD_ID}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert codex_app_server.read_antigravity_endpoint(str(THREAD_ID), home=tmp_path) == server


def test_antigravity_endpoint_prefers_the_host_environment(tmp_path: Path) -> None:
    received = "recorded"
    recorded = codex_app_server.AntigravityServer(address="127.0.0.1:1", csrf_token=received)
    codex_app_server.write_antigravity_endpoint(recorded, thread_id=str(THREAD_ID), home=tmp_path)
    injected = codex_app_server.resolve_antigravity_endpoint(
        str(THREAD_ID),
        home=tmp_path,
        environment={
            "ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:2",
            "ANTIGRAVITY_CSRF_TOKEN": "injected",
        },
        scan=_no_scan,
    )
    injected_csrf = "injected"
    assert injected == codex_app_server.AntigravityServer(
        address="127.0.0.1:2", csrf_token=injected_csrf
    )


def test_antigravity_endpoint_record_serves_a_process_without_the_host_environment(
    tmp_path: Path,
) -> None:
    recorded_csrf = "recorded"
    recorded = codex_app_server.AntigravityServer(address="127.0.0.1:3", csrf_token=recorded_csrf)
    codex_app_server.write_antigravity_endpoint(recorded, thread_id=str(THREAD_ID), home=tmp_path)
    resolved = codex_app_server.resolve_antigravity_endpoint(
        str(THREAD_ID), home=tmp_path, environment={}, scan=_no_scan
    )
    assert resolved == recorded


def test_antigravity_endpoint_failure_names_the_record_it_looked_for(tmp_path: Path) -> None:
    def no_server(**_kwargs: object) -> codex_app_server.AntigravityServer:
        raise RuntimeError("No running Antigravity language_server was found on this machine")

    path = codex_app_server.antigravity_endpoint_path(str(THREAD_ID), home=tmp_path)
    with pytest.raises(RuntimeError) as failure:
        codex_app_server.resolve_antigravity_endpoint(
            str(THREAD_ID), home=tmp_path, environment={}, scan=no_server
        )
    assert str(path) in str(failure.value)
    assert "request records it" in str(failure.value)


def test_another_process_reaches_the_conversation_from_the_record_alone(
    tmp_path: Path,
) -> None:
    # meiva:note
    # The accepting runtime has no ANTIGRAVITY_* variables, so the record is its only
    # route: this real child process resolves the endpoint the request recorded and
    # calls it, which is the hop both the relay and the ended-lease notice make.
    # meiva:link docs/conventions/agents/agent-impersonation-hosts.md
    httpd, seen, thread = _antigravity_http_stub()
    port = httpd.server_address[1]
    csrf = "recorded-csrf"
    recorded = codex_app_server.AntigravityServer(address=f"127.0.0.1:{port}", csrf_token=csrf)
    codex_app_server.write_antigravity_endpoint(recorded, thread_id=str(THREAD_ID), home=tmp_path)
    probe = Path(__file__).parent / "antigravity_endpoint_probe.py"
    root = Path(__file__).resolve().parents[4]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "MEIVA_HOME": str(tmp_path),
        "PYTHONPATH": str(root),
    }
    try:
        completed = subprocess.run(  # noqa: S603
            [sys.executable, str(probe), str(THREAD_ID)],
            capture_output=True,
            text=True,
            env=env,
            cwd=root,
            timeout=120,
            check=False,
        )
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()
    assert completed.returncode == 0, completed.stderr
    assert seen == [csrf]
