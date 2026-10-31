"""`meiva.ui.show` / `.serve` / `.close` SDK entry point tests (R3 door ③).

show/close go through in-process FastAPI TestClient: SDK calls hit gateway
endpoints directly, using a real meiva_test DB to exercise the full wire
protocol. serve() declares the page (writes the agent_pages row + records
serve_dir) and waits for the page_server daemon's server to answer on the
port — in tests, a stub HTTP server stands in for the daemon's spawn.

(`meiva.ui.notify` is registered by the meiva_fleet plugin; tests are in
meiva_builtins/plugins/meiva_fleet/tests/test_meiva_fleet_plugin.py.)
"""

from __future__ import annotations

import http.server
import math
import socket
import socketserver
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

import meiva
from base.agents.tests.units import spawn_agent
from base.clock import SYSTEM_TIMERS
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.host.env import port_lease
from base.lm.catalog import ModelCatalog
from gateway.tests.auth.gateway_config import gateway_test_client
from meiva import ui
from meiva.gateway_client import use_client
from meiva.sdk_surface import agent_identity
from meiva.tests.fixtures.pin_agent import pin_agent
from meiva.ui import InvalidPageName, PageClosed

_HOST = "127.0.0.1"  # loopback — the single-box posture the SDK registers (audit P1-4: only loopback / the agent's own machine are legal proxy targets)


@pytest.fixture(autouse=True)
def _sdk_via_inprocess_gateway(
    monkeypatch: pytest.MonkeyPatch, machine_identity: ConfiguredMachineIdentity
):

    machine_identity.set(host=_HOST)
    with (
        gateway_test_client(base_url="http://test-gateway", machine=machine_identity) as tc,
        use_client(tc),
    ):
        yield


# meiva:note
# A page host for the current agent with a custom ready timeout.
def _page_host(
    *, local_machine: MachineIdentityReader, ready_timeout_s: float
) -> meiva.ui.PageHost:
    return meiva.ui.PageHost(
        host=local_machine.host(),
        agent_id=agent_identity.require_agent_id(),
        timers=SYSTEM_TIMERS,
        ready_timeout_s=ready_timeout_s,
    )


# meiva:note
# (created_at, expires_at) of the agent's open page `name`.
def _expiry_window(db: psycopg.Connection, agent_id: int, name: str) -> tuple[datetime, datetime]:
    db.rollback()
    row = db.execute(
        "SELECT created_at, expires_at FROM agent_pages "
        "WHERE agent_id = %s AND name = %s AND closed_at IS NULL",
        (agent_id, name),
    ).fetchone()
    assert row is not None and row[1] is not None
    return row[0], row[1]


def _seconds_between(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds()


def _open_pages(db: psycopg.Connection, agent_id: int) -> list[tuple[Any, ...]]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT name, port, title, serve_dir FROM agent_pages "
            "WHERE agent_id = %s AND closed_at IS NULL ORDER BY id",
            (agent_id,),
        )
        return cur.fetchall()


class _StubPageServer:
    """A real HTTP server answering /health — the stand-in for what the
    page_server daemon spawns for an open row.

    `health=None` makes it answer 404 on /health instead: the shape of a plain
    `python -m http.server`, i.e. an HTTP server that is *not* a page server —
    the foreign occupier serve() must refuse.
    """

    def __init__(self, host: str, port: int = 0, *, health: bytes | None = b"ok:stub") -> None:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path != "/health" or health is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(health)

            def log_message(self, format: str, *args: object) -> None:
                pass

        class _ReuseServer(socketserver.ThreadingTCPServer):
            allow_reuse_address = True

        self._server = _ReuseServer((host, port), Handler)
        self.port = self._server.server_address[1]
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _StubPageServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


class _SilentServer:
    """Reserve a loopback port while deliberately withholding an HTTP response."""

    def __init__(self, host: str, port: int = 0) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind((host, port))
        self._socket.listen()
        self.port = self._socket.getsockname()[1]

    def __enter__(self) -> _SilentServer:
        return self

    def __exit__(self, *exc: object) -> None:
        self._socket.close()


# meiva:note
# A loopback port number this session holds and nothing listens on.
def _free_port() -> int:
    return port_lease.lease_ports(1)[0]


class TestShow:
    def test_show_registers_page(
        self,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        pin_agent(
            spawn_agent(
                catalog=model_catalog,
                authority=config_authority,
                database_gate=database_gate,
                machine=machine_identity,
            )
        )
        page = meiva.ui.show("cleanup", 13580, title="Picker")
        assert page.name == "cleanup"
        assert page.port == 13580
        assert page.title == "Picker"
        assert (
            page.url
            == f"http://test-gateway.invalid:8000/pages/{agent_identity.require_agent_id()}-cleanup/"
        )
        db_conn.rollback()
        assert _open_pages(db_conn, agent_identity.require_agent_id()) == [
            ("cleanup", 13580, "Picker", None)
        ]

    def test_show_same_name_auto_closes_previous(
        self,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """Single page per agent: re-showing auto-closes the old page. The
        agent's own row is never a port conflict for itself, even unchanged."""
        pin_agent(
            spawn_agent(
                catalog=model_catalog,
                authority=config_authority,
                database_gate=database_gate,
                machine=machine_identity,
            )
        )
        meiva.ui.show("p1", 13581)
        meiva.ui.show("p2", 13581)
        db_conn.rollback()
        # Only p2 is open; p1 was auto-closed (single page per agent).
        assert _open_pages(db_conn, agent_identity.require_agent_id()) == [
            ("p2", 13581, None, None)
        ]

    def test_show_custom_port(
        self,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """The caller's explicit port is what lands in the registry and the page."""
        pin_agent(
            spawn_agent(
                catalog=model_catalog,
                authority=config_authority,
                database_gate=database_gate,
                machine=machine_identity,
            )
        )
        page = meiva.ui.show("custom", 13579, title="Custom")
        assert page.port == 13579
        assert (
            page.url
            == f"http://test-gateway.invalid:8000/pages/{agent_identity.require_agent_id()}-custom/"
        )
        db_conn.rollback()
        assert _open_pages(db_conn, agent_identity.require_agent_id()) == [
            ("custom", 13579, "Custom", None)
        ]

    def test_show_invalid_name_raises(
        self,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        pin_agent(
            spawn_agent(
                catalog=model_catalog,
                authority=config_authority,
                database_gate=database_gate,
                machine=machine_identity,
            )
        )
        with pytest.raises(InvalidPageName):
            meiva.ui.show("bad/name", 13582)
        with pytest.raises(InvalidPageName):
            meiva.ui.show("", 13582)

    def test_show_requires_explicit_port(
        self,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """No per-agent reserved port: a missing port is rejected, with the
        rule spelled out, instead of falling back to a computed value."""
        pin_agent(
            spawn_agent(
                catalog=model_catalog,
                authority=config_authority,
                database_gate=database_gate,
                machine=machine_identity,
            )
        )
        with pytest.raises(TypeError, match="port"):
            meiva.ui.show("page")  # pyright: ignore[reportCallIssue]
        with pytest.raises(TypeError, match="port is required"):
            meiva.ui.show("page", None)  # pyright: ignore[reportArgumentType]

    def test_show_rejects_out_of_range_port(
        self,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """Privileged and out-of-range ports fail at the SDK boundary."""
        pin_agent(
            spawn_agent(
                catalog=model_catalog,
                authority=config_authority,
                database_gate=database_gate,
                machine=machine_identity,
            )
        )
        with pytest.raises(ValueError, match="out of range"):
            meiva.ui.show("page", 80)
        with pytest.raises(ValueError, match="out of range"):
            meiva.ui.show("page", 65536)

    def test_show_passes_integer_ttl_to_gateway(
        self,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        agent_id = spawn_agent(
            catalog=model_catalog,
            authority=config_authority,
            database_gate=database_gate,
            machine=machine_identity,
        )
        pin_agent(agent_id)
        meiva.ui.show("timed", port=12000, ttl=12.9)
        created_at, expires_at = _expiry_window(db_conn, agent_id, "timed")
        assert _seconds_between(created_at, expires_at) == pytest.approx(12, abs=0.5)

    def test_show_without_ttl_omits_gateway_field(
        self,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        agent_id = spawn_agent(
            catalog=model_catalog,
            authority=config_authority,
            database_gate=database_gate,
            machine=machine_identity,
        )
        pin_agent(agent_id)
        meiva.ui.show("plain", port=12000)
        created_at, expires_at = _expiry_window(db_conn, agent_id, "plain")
        # meiva:note
        # No ttl on the wire: the gateway applies its configured default (86400s).
        assert _seconds_between(created_at, expires_at) == pytest.approx(86400, abs=0.5)


@pytest.mark.parametrize("ttl", [0.0, -1.0, math.inf, math.nan])
@pytest.mark.parametrize("api", ["show", "serve"])
def test_page_apis_reject_invalid_ttl(
    api: str,
    ttl: float,
    tmp_path: Path,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    pin_agent(
        spawn_agent(
            catalog=model_catalog,
            authority=config_authority,
            database_gate=database_gate,
            machine=machine_identity,
        )
    )
    call = getattr(meiva.ui, api)
    args = (str(tmp_path), "page", 13584)
    if api == "show":
        args = ("page", 13584)
    with pytest.raises(ValueError, match="ttl"):
        call(*args, ttl=ttl)


class TestClose:
    def test_close_marks_closed(
        self,
        db_conn: psycopg.Connection,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        pin_agent(
            spawn_agent(
                catalog=model_catalog,
                authority=config_authority,
                database_gate=database_gate,
                machine=machine_identity,
            )
        )
        meiva.ui.show("p", 13583)
        meiva.ui.close("p")
        db_conn.rollback()
        assert _open_pages(db_conn, agent_identity.require_agent_id()) == []

    def test_close_missing_raises_page_closed(
        self,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        pin_agent(
            spawn_agent(
                catalog=model_catalog,
                authority=config_authority,
                database_gate=database_gate,
                machine=machine_identity,
            )
        )
        with pytest.raises(PageClosed):
            meiva.ui.close("never-registered")


class TestServe:
    """serve() declares the page (row + serve_dir) and waits for the
    page_server daemon's server to answer on the port — no process is
    spawned by the SDK anymore (R3 door ③)."""

    @pytest.fixture(autouse=True)
    def _loopback_identity(self, machine_identity: ConfiguredMachineIdentity):
        """serve() polls reachable_host() — pin it to loopback so the stub
        server can actually bind it."""
        machine_identity.set(host="127.0.0.1")
        yield

    def test_serve_registers_serve_dir(
        self,
        db_conn: psycopg.Connection,
        tmp_path: Path,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        pin_agent(
            spawn_agent(
                catalog=model_catalog,
                authority=config_authority,
                database_gate=database_gate,
                machine=machine_identity,
            )
        )
        (tmp_path / "index.html").write_text("<h1>served</h1>", encoding="utf-8")
        with _StubPageServer("127.0.0.1") as stub:
            page = meiva.ui.serve(str(tmp_path), "srv", port=stub.port, title="Served")
        assert page.name == "srv"
        assert page.port == stub.port
        db_conn.rollback()
        assert _open_pages(db_conn, agent_identity.require_agent_id()) == [
            ("srv", stub.port, "Served", str(tmp_path))
        ]

    def test_serve_passes_ttl(
        self,
        db_conn: psycopg.Connection,
        tmp_path: Path,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        agent_id = spawn_agent(
            catalog=model_catalog,
            authority=config_authority,
            database_gate=database_gate,
            machine=machine_identity,
        )
        pin_agent(agent_id)
        with _StubPageServer("127.0.0.1") as stub:
            meiva.ui.serve(str(tmp_path), "srv-ttl", port=stub.port, ttl=30)
        created_at, expires_at = _expiry_window(db_conn, agent_id, "srv-ttl")
        assert _seconds_between(created_at, expires_at) == pytest.approx(30, abs=0.5)

    def test_serve_waits_for_server_to_come_up(
        self,
        db_conn: psycopg.Connection,
        tmp_path: Path,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """serve() polls until the daemon's server answers (stub starts late)."""
        pin_agent(
            spawn_agent(
                catalog=model_catalog,
                authority=config_authority,
                database_gate=database_gate,
                machine=machine_identity,
            )
        )
        (tmp_path / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
        with _StubPageServer("127.0.0.1") as stub:
            page = meiva.ui.serve(str(tmp_path), "late", port=stub.port)
        assert page.port == stub.port

    def test_serve_times_out_without_daemon(
        self,
        db_conn: psycopg.Connection,
        tmp_path: Path,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """A free port the daemon never binds -> serve() raises after the wait
        window (the page row stays registered; the daemon keeps retrying)."""
        import meiva.ui as ui_mod

        pin_agent(
            spawn_agent(
                catalog=model_catalog,
                authority=config_authority,
                database_gate=database_gate,
                machine=machine_identity,
            )
        )
        (tmp_path / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
        host = _page_host(ready_timeout_s=0.5, local_machine=machine_identity)
        free = _free_port()
        with pytest.raises(ui_mod.PageError, match="did not come up"):
            host.serve(ui.serve_request(str(tmp_path), "nod", free, None, None))
        db_conn.rollback()
        # The declaration is durable even when the server is not up yet.
        assert _open_pages(db_conn, agent_identity.require_agent_id()) == [
            ("nod", free, None, str(tmp_path))
        ]

    def test_serve_requires_explicit_port(
        self,
        tmp_path: Path,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """No per-agent reserved port: a missing port is rejected before any
        gateway or filesystem work, with the rule spelled out."""
        pin_agent(
            spawn_agent(
                catalog=model_catalog,
                authority=config_authority,
                database_gate=database_gate,
                machine=machine_identity,
            )
        )
        with pytest.raises(TypeError, match="port"):
            meiva.ui.serve(str(tmp_path), "page")  # pyright: ignore[reportCallIssue]
        with pytest.raises(TypeError, match="port is required"):
            meiva.ui.serve(str(tmp_path), "page", None)  # pyright: ignore[reportArgumentType]

    def test_serve_rejects_port_held_by_foreign_process(
        self,
        db_conn: psycopg.Connection,
        tmp_path: Path,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """A non-page listener on the port fails serve() before anything is
        registered — the daemon can never displace it."""
        import meiva.ui as ui_mod

        pin_agent(
            spawn_agent(
                catalog=model_catalog,
                authority=config_authority,
                database_gate=database_gate,
                machine=machine_identity,
            )
        )
        (tmp_path / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
        with (
            _SilentServer("127.0.0.1") as silent,
            pytest.raises(ui_mod.PageError, match="already in use"),
        ):
            meiva.ui.serve(str(tmp_path), "blocked", port=silent.port)
        db_conn.rollback()
        assert _open_pages(db_conn, agent_identity.require_agent_id()) == []

    def test_serve_rejects_port_held_by_non_page_http_server(
        self,
        db_conn: psycopg.Connection,
        tmp_path: Path,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """An HTTP server that is not a page server (no `ok:` /health) is the
        same foreign occupant — refused, not silently registered."""
        import meiva.ui as ui_mod

        pin_agent(
            spawn_agent(
                catalog=model_catalog,
                authority=config_authority,
                database_gate=database_gate,
                machine=machine_identity,
            )
        )
        (tmp_path / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
        with (
            _StubPageServer("127.0.0.1", health=None) as foreign,
            pytest.raises(ui_mod.PageError, match="already in use"),
        ):
            meiva.ui.serve(str(tmp_path), "blocked", port=foreign.port)
        db_conn.rollback()
        assert _open_pages(db_conn, agent_identity.require_agent_id()) == []

    def test_serve_rejects_port_held_by_another_agents_page(
        self,
        db_conn: psycopg.Connection,
        tmp_path: Path,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """Registry conflict: another agent's live page owns the port — the
        error names the occupying page, and nothing is registered."""
        import meiva.ui as ui_mod

        owner = spawn_agent(
            catalog=model_catalog,
            authority=config_authority,
            database_gate=database_gate,
            machine=machine_identity,
        )
        pin_agent(owner)
        meiva.ui.show("held", 13586)
        pin_agent(
            spawn_agent(
                catalog=model_catalog,
                authority=config_authority,
                database_gate=database_gate,
                machine=machine_identity,
            )
        )
        (tmp_path / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
        with pytest.raises(ui_mod.PageError, match="already used by page 'held' of agent"):
            meiva.ui.serve(str(tmp_path), "clash", port=13586)
        db_conn.rollback()
        assert [r[0] for r in _open_pages(db_conn, owner)] == ["held"]
        assert _open_pages(db_conn, agent_identity.require_agent_id()) == []

    def test_serve_refusal_leaves_current_page_untouched(
        self,
        db_conn: psycopg.Connection,
        tmp_path: Path,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """A refused serve() (port taken by another agent) must not close the
        agent's own current page."""
        import meiva.ui as ui_mod

        owner = spawn_agent(
            catalog=model_catalog,
            authority=config_authority,
            database_gate=database_gate,
            machine=machine_identity,
        )
        other = spawn_agent(
            catalog=model_catalog,
            authority=config_authority,
            database_gate=database_gate,
            machine=machine_identity,
        )
        pin_agent(owner)
        meiva.ui.show("mine", 13587)
        pin_agent(other)
        meiva.ui.show("theirs", 13588)
        pin_agent(owner)
        (tmp_path / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
        with pytest.raises(ui_mod.PageError, match="already used by page 'theirs'"):
            meiva.ui.serve(str(tmp_path), "new", port=13588)
        db_conn.rollback()
        assert [r[0] for r in _open_pages(db_conn, owner)] == ["mine"]
        assert [r[0] for r in _open_pages(db_conn, other)] == ["theirs"]

    def test_serve_allows_replacing_own_page_with_port_still_bound(
        self,
        db_conn: psycopg.Connection,
        tmp_path: Path,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """The agent's own page on the port is not a foreign occupant: even
        with its (wedged) server still bound, serve() replaces it — the
        registry check owns that call, and the OS probe must not block it."""
        import meiva.ui as ui_mod

        pin_agent(
            spawn_agent(
                catalog=model_catalog,
                authority=config_authority,
                database_gate=database_gate,
                machine=machine_identity,
            )
        )
        (tmp_path / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
        host = _page_host(ready_timeout_s=0.5, local_machine=machine_identity)
        with _SilentServer("127.0.0.1") as silent:
            meiva.ui.show("wedged", silent.port)
            with pytest.raises(ui_mod.PageError, match="did not come up"):
                host.serve(ui.serve_request(str(tmp_path), "wedged2", silent.port, None, None))
        db_conn.rollback()
        # The replacement row was registered; only the (stub) server never came up.
        assert [r[0] for r in _open_pages(db_conn, agent_identity.require_agent_id())] == [
            "wedged2"
        ]

    def test_serve_ready_timeout_covers_a_slow_daemon_pass(self) -> None:
        """The serve() wait must exceed the slowest observed daemon pass
        (~30s, 2026-08-28) — the fast path makes the normal path ~2s, but the
        window is the fallback for a slow pass / cold restart."""
        import meiva.ui as ui_mod

        assert ui_mod.PageHost(host="h", agent_id=1, timers=SYSTEM_TIMERS).ready_timeout_s >= 60.0

    # meiva:note
    # The ready window is measured and slept on the timers the host carries: a manual clock
    # spends the window without real seconds, so a test drives the wait by value.
    def test_wait_until_serving_measures_its_window_on_the_clock_it_is_given(self) -> None:
        import meiva.ui as ui_mod
        from base.clock.tests.manual_timers import ManualTimers

        timers = ManualTimers()
        host = ui_mod.PageHost(host=_HOST, agent_id=1, ready_timeout_s=0.5, timers=timers)

        assert host.wait_until_serving(_free_port()) is False
        assert timers.sleeps == [0.2, 0.2, 0.2]

    def test_serve_same_name_replaces(
        self,
        db_conn: psycopg.Connection,
        tmp_path: Path,
        *,
        model_catalog: ModelCatalog,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        pin_agent(
            spawn_agent(
                catalog=model_catalog,
                authority=config_authority,
                database_gate=database_gate,
                machine=machine_identity,
            )
        )
        (tmp_path / "index.html").write_text("<h1>x</h1>", encoding="utf-8")
        with _StubPageServer("127.0.0.1") as stub:
            meiva.ui.serve(str(tmp_path), "once", port=stub.port)
            meiva.ui.serve(str(tmp_path), "twice", port=stub.port)
        db_conn.rollback()
        assert [r[0] for r in _open_pages(db_conn, agent_identity.require_agent_id())] == ["twice"]
