# meiva:note
# `meiva auth set-password`: what it forwards to the gateway, against a listener that plays the
# gateway. The gateway owns the rules; here the command is judged on what it sends, what it refuses
# to send and what it tells the operator.

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from base.cluster.machine import GatewayAccess
from cli.commands.auth import cmd_auth_set_password

# meiva:note
# A fixture value, not a credential.
_SECRET = "human-secret-for-auth-tests"  # noqa: S105
# meiva:note
# A fixture value, not a credential.
_MACHINE_TOKEN = "machine-token-0001"  # noqa: S105
# meiva:note
# A fixture value, not a credential.
_PASSWORD = "a long enough password"  # noqa: S105


@dataclass
class PasswordGateway:
    status: int = 200
    body: dict[str, object] = field(default_factory=lambda: {"ok": True})
    url: str = ""
    calls: list[tuple[str, str, str | None, object]] = field(
        default_factory=list[tuple[str, str, str | None, object]]
    )


@contextmanager
def _gateway(
    *, status: int = 200, body: dict[str, object] | None = None
) -> Generator[PasswordGateway]:
    gateway = PasswordGateway(status=status) if body is None else PasswordGateway(status, body)

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self) -> None:
            sent = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            gateway.calls.append((self.command, self.path, self.headers.get("Authorization"), sent))
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


def _access(url: str, *, secret: str = _SECRET, machine_token: str | None = None) -> GatewayAccess:
    return GatewayAccess(
        gateway_url=url,
        cluster_secret=secret,
        remote_plane=False,
        machine_token=machine_token,
        launcher_profile=None,
        gateway_home=True,
    )


def test_the_password_goes_to_the_gateway_as_the_cluster_secret_never_to_the_screen(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _gateway() as gateway:
        status = cmd_auth_set_password(_access(gateway.url), _PASSWORD)

    assert status == 0
    assert gateway.calls == [
        ("PUT", "/api/auth/password", f"Bearer {_SECRET}", {"password": _PASSWORD})
    ]
    captured = capsys.readouterr()
    assert "web login password set" in captured.out
    assert _PASSWORD not in captured.out + captured.err
    assert _SECRET not in captured.out + captured.err


# meiva:note
# The gateway refuses a machine token here (it cannot choose what a browser logs in with),
# so the request carries the human secret even when the process holds a token too.
def test_a_delivered_machine_token_is_never_the_bearer() -> None:
    with _gateway() as gateway:
        cmd_auth_set_password(_access(gateway.url, machine_token=_MACHINE_TOKEN), _PASSWORD)

    assert [call[2] for call in gateway.calls] == [f"Bearer {_SECRET}"]


def test_a_process_without_the_cluster_secret_asks_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _gateway() as gateway:
        status = cmd_auth_set_password(
            _access(gateway.url, secret="", machine_token=_MACHINE_TOKEN), _PASSWORD
        )

    assert status == 1
    assert gateway.calls == []
    assert "gateway's own home" in capsys.readouterr().err


def test_a_refusal_is_reported_with_the_gateways_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    refusal: dict[str, object] = {
        "code": "password_too_short",
        "detail": "the password must be at least 12 characters",
    }
    with _gateway(status=422, body=refusal) as gateway:
        status = cmd_auth_set_password(_access(gateway.url), "short")

    assert status == 1
    err = capsys.readouterr().err
    assert "HTTP 422" in err and "at least 12 characters" in err
    assert "short" not in err.replace("the password must be at least 12 characters", "")


def test_a_refusal_without_a_json_reason_is_still_reported(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with _gateway(status=403, body={"unexpected": True}) as gateway:
        assert cmd_auth_set_password(_access(gateway.url), _PASSWORD) == 1

    assert "HTTP 403" in capsys.readouterr().err


def test_an_unreachable_gateway_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    with _gateway() as gateway:
        unreachable = gateway.url

    assert cmd_auth_set_password(_access(unreachable), _PASSWORD) == 1
    assert "unreachable" in capsys.readouterr().err
