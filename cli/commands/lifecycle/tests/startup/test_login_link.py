# meiva:note
# `meiva start`'s one-time browser login link, against a real local gateway.
#
# The link is `<ui>/#ticket=<ticket>`: the ticket is minted by the gateway for the home's human
# secret and travels in the fragment. A start that is not at a terminal, has no UI, or holds no
# human secret mints nothing; a gateway failure is reported without failing the start. Whether to
# mint is decided from values (`../start_plans/test_login_link_plan.py`); here the shell dials a
# listener that plays the gateway.

from __future__ import annotations

import dataclasses
import json
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from cli.commands.lifecycle.start.login_link import print_login_link
from cli.commands.lifecycle.start.start_config import StartConfig, read_start_config
from cli.database.runtime import CommandRuntime

_SECRET = "human-secret-for-link-tests"  # noqa: S105 — test fixture
_TICKET = "ticket.human-0123.abcDEF_-xyz"


# meiva:note
# A listener on port 0 that answers `POST /api/auth/login-ticket` and records the call.
@dataclass
class TicketGateway:
    status: int = 200
    body: dict[str, object] = field(
        default_factory=lambda: {"ticket": _TICKET, "expires_in_seconds": 120}
    )
    url: str = ""
    calls: list[tuple[str, str | None]] = field(default_factory=list[tuple[str, str | None]])


@contextmanager
def _gateway(
    *, status: int = 200, body: dict[str, object] | None = None
) -> Generator[TicketGateway]:
    gateway = TicketGateway(status=status) if body is None else TicketGateway(status, body)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            gateway.calls.append((self.path, self.headers.get("Authorization")))
            payload = json.dumps(gateway.body).encode()
            self.send_response(gateway.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    gateway.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield gateway
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _config(command: CommandRuntime, gateway_url: str, *, secret: str = _SECRET) -> StartConfig:
    config = read_start_config(command.config)
    return dataclasses.replace(
        config,
        gateway_url=gateway_url,
        browser_origin="",
        entry_url="http://localhost:3000",
        app_port=None,
        data_plane=dataclasses.replace(config.data_plane, cluster_secret=secret),
    )


def test_prints_the_ticket_in_the_fragment_and_never_the_secret(
    command_runtime: CommandRuntime, capsys: pytest.CaptureFixture[str]
) -> None:
    with _gateway() as gateway:
        config = _config(command_runtime, gateway.url)

        print_login_link({"gateway", "gate"}, config=config, at_terminal=True)

    out = capsys.readouterr().out
    assert f"http://localhost:3000/#ticket={_TICKET}" in out
    assert _SECRET not in out
    assert gateway.calls == [("/api/auth/login-ticket", f"Bearer {_SECRET}")]


def test_a_ticket_is_made_safe_for_a_url_fragment(
    command_runtime: CommandRuntime, capsys: pytest.CaptureFixture[str]
) -> None:
    with _gateway(body={"ticket": "a b/c?d#e"}) as gateway:
        print_login_link({"gate"}, config=_config(command_runtime, gateway.url), at_terminal=True)

    assert "/#ticket=a%20b%2Fc%3Fd%23e" in capsys.readouterr().out


def test_skips_when_output_is_not_a_terminal(
    command_runtime: CommandRuntime, capsys: pytest.CaptureFixture[str]
) -> None:
    with _gateway() as gateway:
        print_login_link(
            {"gateway", "gate"}, config=_config(command_runtime, gateway.url), at_terminal=False
        )

    out = capsys.readouterr().out
    assert gateway.calls == []
    assert "#ticket=" not in out and "skipped" in out


def test_mints_nothing_without_a_ui_or_the_human_secret(
    command_runtime: CommandRuntime, capsys: pytest.CaptureFixture[str]
) -> None:
    with _gateway() as gateway:
        config = _config(command_runtime, gateway.url)
        print_login_link({"gateway"}, config=config, at_terminal=True)
        print_login_link(
            {"gateway", "gate"},
            config=_config(command_runtime, gateway.url, secret=""),
            at_terminal=True,
        )

    assert gateway.calls == []
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (403, {"detail": "requires the human cluster secret"}),
        (200, {"ok": True}),
    ],
    ids=["refused", "no-ticket"],
)
def test_a_gateway_failure_is_reported_and_does_not_raise(
    command_runtime: CommandRuntime,
    capsys: pytest.CaptureFixture[str],
    status: int,
    body: dict[str, object],
) -> None:
    with _gateway(status=status, body=body) as gateway:
        print_login_link(
            {"gateway", "gate"}, config=_config(command_runtime, gateway.url), at_terminal=True
        )

    captured = capsys.readouterr()
    assert "could not mint a one-time UI login link" in captured.err
    assert "#ticket=" not in captured.out


def test_an_unreachable_gateway_is_reported_and_does_not_raise(
    command_runtime: CommandRuntime, capsys: pytest.CaptureFixture[str]
) -> None:
    with _gateway() as gateway:
        unreachable = gateway.url
    config = _config(command_runtime, unreachable)

    print_login_link({"gateway", "gate"}, config=config, at_terminal=True)

    captured = capsys.readouterr()
    assert "could not mint a one-time UI login link" in captured.err
    assert "#ticket=" not in captured.out
