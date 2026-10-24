# meiva:note
# `meiva stop` tells the gateway and the host's own boot job that the stop is on purpose.
#
# The gateway is a real HTTP listener the stop dials, the root a real control socket: what the
# stop sent and in which order is read from what they received.

from __future__ import annotations

import io
import json
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from base.host.tests.short_tmp import short_tmp as short_tmp
from base.telemetry import EventPipeline
from cli.commands.lifecycle import stand_down
from cli.commands.lifecycle.stop import do_stop
from cli.commands.lifecycle.stop.choices import StopWindow
from cli.commands.lifecycle.tests.scripted_root import UnitRow, scripted_root
from cli.commands.lifecycle.tests.stop.stop_support import stop_environment, stop_request
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.cli_tests import operator_pipeline as operator_pipeline
from cli.database.tests.command_inputs import command_runtime as command_runtime

# meiva:note
# The secret is a literal of the throwaway gateway this test serves.
SECRET = "test-cluster-secret"  # noqa: S105


# meiva:note
# A gateway that records the requests dialed at it, with the clock they arrived on.
@dataclass
class Gateway:
    url: str
    requests: list[tuple[float, str, dict[str, str], str | None]] = field(
        default_factory=list[tuple[float, str, dict[str, str], str | None]]
    )


@contextmanager
def gateway_listening() -> Generator[Gateway]:
    gateway = Gateway(url="")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            parts = urlsplit(self.path)
            query = {key: values[0] for key, values in parse_qs(parts.query).items()}
            gateway.requests.append(
                (time.monotonic(), parts.path, query, self.headers.get("Authorization"))
            )
            body = json.dumps({"machine": query.get("machine")}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    gateway.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield gateway
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def stopping(command_runtime: CommandRuntime, short_tmp: Path) -> CommandRuntime:
    """A unit named `test-host` whose root answers under a short home."""
    command_runtime.machine.set(role="agent-runner", name="test-host")
    command_runtime.config.set_field("cluster_secret", SECRET)
    return replace(command_runtime, home=short_tmp, helper=None)


def pointed_at(runtime: CommandRuntime, gateway_url: str) -> CommandRuntime:
    runtime.config.set_field("gateway_url", gateway_url)
    return runtime


UNITS = ("agent-host", "pty-sessions")


# meiva:note
# A confirmed `meiva stop` best-effort POSTs /api/cluster/stopping?machine=<self>&home=<self-home>
# before the local teardown (so the cluster view shows 'stopped', not 'offline'). `home`
# identifies THIS unit so a co-located peer keeps its caps.
def test_a_confirmed_stop_announces_after_the_confirm_and_before_the_teardown(
    stopping: CommandRuntime,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    with gateway_listening() as gateway, scripted_root(stopping.home, map(UnitRow, UNITS)) as root:
        rc = do_stop(
            stop_request(announce=True, keep_shell_sessions=True, require_confirmation=True),
            stop_environment(
                pointed_at(stopping, gateway.url),
                operator_database,
                operator_pipeline,
                stdin=io.StringIO("y\n"),
            ),
            retained_children=[],
        )
    assert rc == 0
    ((announced_at, path, query, authorization),) = gateway.requests
    assert path == "/api/cluster/stopping"
    assert query == {"machine": "test-host", "home": str(stopping.home)}
    assert authorization == f"Bearer {SECRET}"
    first_teardown = next(at for event, at in root.events if event.startswith("down"))
    assert announced_at < first_teardown, "announced first, then teardown ran"
    held = stand_down.read(stopping.home)
    assert held is not None and held.machine == "test-host" and held.until is None


# meiva:note
# Declining the confirm prompt must leave the roster untouched: the stopping announce stamps
# `machines.stopped_at`, and only the next `meiva start` clears it — an announce fired before
# the gate would mark a running host 'stopped'.
@pytest.mark.parametrize("answer", ["n\n", "", "\n", "maybe\n"])
def test_a_declined_confirm_leaves_the_roster_and_the_unit_untouched(
    answer: str,
    stopping: CommandRuntime,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    with gateway_listening() as gateway, scripted_root(stopping.home, map(UnitRow, UNITS)) as root:
        rc = do_stop(
            stop_request(announce=True, require_confirmation=True),
            stop_environment(
                pointed_at(stopping, gateway.url),
                operator_database,
                operator_pipeline,
                stdin=io.StringIO(answer),
            ),
            retained_children=[],
        )
    assert rc == 0
    assert gateway.requests == [], "no announce"
    assert [call for call in root.calls if call[0] != "status"] == [], "no teardown"
    assert stand_down.read(stopping.home) is None, "no stop was declared"
    out = capsys.readouterr().out
    assert "confirm? [y/N] " in out and "aborted" in out


def test_a_stop_that_asks_for_confirmation_needs_a_stream_to_read_it_from(
    stopping: CommandRuntime,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    with pytest.raises(ValueError, match="needs a stdin"):
        do_stop(
            stop_request(require_confirmation=True),
            stop_environment(stopping, operator_database, operator_pipeline),
            retained_children=[],
        )


# meiva:note
# If the stopping announce can't reach the gateway, `meiva stop` logs and still tears down:
# the announce is best-effort, never a blocker. The local half of the intent is written first,
# so an unreachable gateway costs the record nothing.
def test_the_stop_proceeds_when_the_announce_cannot_reach_the_gateway(
    stopping: CommandRuntime,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    with scripted_root(stopping.home, map(UnitRow, UNITS)) as root:
        rc = do_stop(
            stop_request(announce=True, keep_shell_sessions=True),
            stop_environment(
                pointed_at(stopping, "http://127.0.0.1:1"), operator_database, operator_pipeline
            ),
            retained_children=[],
        )
    assert rc == 0
    assert ("down", "agent-host") in root.calls, "teardown still ran"
    assert "could not announce shutdown" in capsys.readouterr().out
    assert stand_down.read(stopping.home) is not None


# meiva:note
# `meiva stop --maintenance-for 10m --reason ...`: the window goes to the gateway so the
# cluster view says why the unit is down, and to the home's own record so the OS boot job
# leaves it alone until then.
def test_a_declared_window_reaches_the_gateway_and_the_homes_stand_down_record(
    stopping: CommandRuntime,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    window = StopWindow(for_seconds=600, reason="disk swap")
    with gateway_listening() as gateway, scripted_root(stopping.home, map(UnitRow, UNITS)) as root:
        rc = do_stop(
            stop_request(announce=True, force=True, keep_shell_sessions=True, window=window),
            stop_environment(
                pointed_at(stopping, gateway.url), operator_database, operator_pipeline
            ),
            retained_children=[],
        )
    assert rc == 0
    ((_at, _path, query, _auth),) = gateway.requests
    assert query["maintenance_for_seconds"] == "600" and query["maintenance_reason"] == "disk swap"
    held = stand_down.read(stopping.home)
    assert held is not None and held.reason == "disk swap" and held.until is not None
    assert ("force-down", "agent-host") in root.calls, "--mode force reached the root"
    assert ("force-down", "pty-sessions") not in root.calls, "--keep-shell-sessions was honoured"
