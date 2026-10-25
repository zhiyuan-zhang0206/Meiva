"""Unit tests for services/desktop/browser.session — the CDP gateway-session injector.

The CDP and HTTP surfaces are faked; nothing here dials a real browser. Also
covers the browser-mcp daemon's gateway-session wiring (best-effort guards,
refresh loop) as implemented by ``services.desktop.browser.gateway_session``.
"""

from __future__ import annotations

import asyncio
import http
import itertools
import json
import socket
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import websockets.asyncio.server
from websockets.asyncio.server import ServerConnection
from websockets.http11 import Request, Response

from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.cluster.machine import GatewayAccess
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from services.desktop.browser import gateway_session
from services.desktop.browser import session as sess
from services.desktop.browser.gateway_session import GatewaySession

SECRET = "test-cluster-secret"  # noqa: S105
GATEWAY = "http://gateway.example:8000"


def _fixed_clock(now_ts: float = 1_700_000_000.0) -> Clock:
    return Clock(
        ClockConfig(
            timezone="UTC",
            authoritative_timezone="UTC",
            message_timestamp_weekday=False,
        ),
        timers=SYSTEM_TIMERS,
        now=lambda: datetime.fromtimestamp(now_ts, tz=UTC),
    )


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeWS:
    """A websocket that answers queued CDP responses and records requests."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.sent: list[dict[str, Any]] = []

    async def __aenter__(self) -> FakeWS:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        return json.dumps(self._responses.pop(0))


# meiva:note
# Loopback HTTP + WebSocket server faking the gateway login and Chrome CDP.
class _LoopbackSessionStack:
    def __init__(
        self,
        *,
        login_status: int = 200,
        set_cookie: str | None = "meiva_session=opaque-session; HttpOnly; Max-Age=86400; Path=/",
        cookie_accepted: bool = True,
    ) -> None:
        self.login_status = login_status
        self.set_cookie = set_cookie
        self.cookie_accepted = cookie_accepted
        self.login_calls: list[tuple[str, dict[str, str], dict[str, str]]] = []
        self.browser_sent: list[dict[str, Any]] = []
        self.page_sent: list[dict[str, Any]] = []
        self.stop_on_login: asyncio.Event | None = None
        self.port: int = 0
        self.http_port: int = 0
        self._http_server: asyncio.Server | None = None
        self._ws_server: websockets.asyncio.server.Server | None = None

    @property
    def gateway_url(self) -> str:
        return f"http://127.0.0.1:{self.http_port}"

    def access(
        self,
        *,
        gateway_url: str | None = None,
        cluster_secret: str = SECRET,
        machine_token: str | None = SECRET,
    ) -> GatewayAccess:
        return GatewayAccess(
            gateway_url=self.gateway_url if gateway_url is None else gateway_url,
            cluster_secret=cluster_secret,
            remote_plane=False,
            machine_token=machine_token,
            launcher_profile="runner",
            gateway_home=False,
        )

    @classmethod
    async def start(
        cls,
        *,
        login_status: int = 200,
        set_cookie: str | None = "meiva_session=opaque-session; HttpOnly; Max-Age=86400; Path=/",
        cookie_accepted: bool = True,
    ) -> _LoopbackSessionStack:
        stack = cls(
            login_status=login_status,
            set_cookie=set_cookie,
            cookie_accepted=cookie_accepted,
        )
        ws_server = await websockets.asyncio.server.serve(
            stack._handle_ws,
            "127.0.0.1",
            0,
            process_request=stack._process_ws_http,
        )
        stack._ws_server = ws_server
        ws_sock = next(iter(ws_server.sockets))
        ws_addr: tuple[str, int] = ws_sock.getsockname()
        stack.port = ws_addr[1]
        http_server = await asyncio.start_server(stack._handle_http, "127.0.0.1", 0)
        stack._http_server = http_server
        http_sock = next(iter(http_server.sockets or ()))
        http_addr: tuple[str, int] = http_sock.getsockname()
        stack.http_port = http_addr[1]
        _STACKS.append(stack)
        return stack

    def _process_ws_http(self, conn: ServerConnection, request: Request) -> Response | None:
        if request.path == "/json/version":
            payload = {"webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/browser"}
            resp = conn.respond(http.HTTPStatus.OK, json.dumps(payload))
            resp.headers["Content-Type"] = "application/json"
            return resp
        return None

    async def close(self) -> None:
        if self._ws_server is not None:
            self._ws_server.close()
            await self._ws_server.wait_closed()
        if self._http_server is not None:
            self._http_server.close()
            await self._http_server.wait_closed()

    async def _handle_http(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw_head = await reader.readuntil(b"\r\n\r\n")
            lines = raw_head.decode("latin-1").split("\r\n")
            _method, path, _proto = lines[0].split(" ", 2)
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            content_length = int(headers.get("content-length", "0"))
            raw_body = await reader.readexactly(content_length) if content_length else b""
            raw_json: Any = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            raw_json_obj: object = raw_json
            raw_dict: dict[str, Any] = raw_json if isinstance(raw_json_obj, dict) else {}
            body_json: dict[str, str] = {str(k): str(v) for k, v in raw_dict.items()}
            self.login_calls.append(
                (
                    f"{self.gateway_url}{path}",
                    body_json,
                    {"User-Agent": headers.get("user-agent", "")},
                )
            )
            if self.stop_on_login is not None:
                self.stop_on_login.set()
            cookie_header = (
                f"Set-Cookie: {self.set_cookie}\r\n" if self.set_cookie is not None else ""
            )
            resp = (
                f"HTTP/1.1 {self.login_status} OK\r\n"
                f"{cookie_header}"
                "Content-Length: 0\r\n"
                "Connection: close\r\n\r\n"
            ).encode("latin-1")
            writer.write(resp)
            await writer.drain()
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()

    async def _handle_ws(self, conn: ServerConnection) -> None:
        path = conn.request.path if conn.request is not None else ""
        is_browser = "browser" in path
        with suppress(websockets.ConnectionClosed):
            async for raw_msg in conn:
                data = raw_msg if isinstance(raw_msg, str) else raw_msg.decode("utf-8")
                req = json.loads(data)
                req_id = req["id"]
                method = req["method"]
                if is_browser:
                    self.browser_sent.append(req)
                    result = {"targetId": "t-1"} if method == "Target.createTarget" else {}
                    await conn.send(json.dumps({"id": req_id, "result": result}))
                else:
                    self.page_sent.append(req)
                    await conn.send(
                        json.dumps({"id": req_id, "result": {"success": self.cookie_accepted}})
                    )


_STACKS: list[_LoopbackSessionStack] = []


@pytest.fixture(autouse=True)
async def _cleanup_stacks() -> AsyncIterator[None]:
    try:
        yield
    finally:
        while _STACKS:
            await _STACKS.pop().close()


async def test_gateway_session_login_returns_server_cookie() -> None:
    stack = await _LoopbackSessionStack.start()

    assert sess.parse_login_cookie(200, stack.set_cookie, now=1_700_000_000) == (
        "meiva_session",
        "opaque-session",
        1_700_086_400,
    )
    name, value, expires = await sess.gateway_session_login(stack.gateway_url, SECRET)

    assert (name, value) == ("meiva_session", "opaque-session")
    assert expires > 0
    url, body, headers = stack.login_calls[0]
    assert (url, body) == (f"{stack.gateway_url}/api/auth/login", {"password": SECRET})
    # the managed-browser marker lets the sessions list label this row
    assert headers == {"User-Agent": sess.MANAGED_BROWSER_USER_AGENT}


async def test_gateway_session_login_rejects_non_200() -> None:
    stack = await _LoopbackSessionStack.start(login_status=401)

    with pytest.raises(RuntimeError, match="HTTP 401"):
        await sess.gateway_session_login(stack.gateway_url, SECRET)


@pytest.mark.parametrize(
    "set_cookie",
    [
        None,
        "other=value; Max-Age=86400",
        "meiva_session=opaque-session; Path=/",
        "meiva_session=opaque-session; Max-Age=invalid",
    ],
)
async def test_gateway_session_login_rejects_malformed_cookie(
    set_cookie: str | None,
) -> None:
    stack = await _LoopbackSessionStack.start(set_cookie=set_cookie)

    with pytest.raises(RuntimeError, match="session cookie"):
        await sess.gateway_session_login(stack.gateway_url, SECRET)


async def test_inject_sets_cookie_with_login_flags() -> None:
    stack = await _LoopbackSessionStack.start()
    await sess.inject_session_cookie(
        stack.port,
        stack.gateway_url,
        SECRET,
    )

    # create a throwaway tab, set the cookie on it, close it
    browser_calls = [c["method"] for c in stack.browser_sent]
    assert browser_calls == ["Target.createTarget", "Target.closeTarget"]
    assert stack.browser_sent[0]["params"] == {"url": "about:blank"}
    assert stack.browser_sent[1]["params"] == {"targetId": "t-1"}

    set_cookie = stack.page_sent[0]
    assert set_cookie["method"] == "Network.setCookie"
    params = set_cookie["params"]
    assert params["name"] == "meiva_session"
    assert params["url"] == stack.gateway_url + "/"  # host-only, trailing slash normalized
    assert params["path"] == "/"
    assert params["httpOnly"] is True
    assert params["sameSite"] == "Lax"
    assert params["value"] == "opaque-session"
    assert isinstance(params["expires"], int) and params["expires"] > 0


async def test_inject_raises_when_browser_rejects_cookie() -> None:
    stack = await _LoopbackSessionStack.start(cookie_accepted=False)
    with pytest.raises(RuntimeError, match="rejected"):
        await sess.inject_session_cookie(stack.port, stack.gateway_url, SECRET)


async def test_cdp_call_raises_on_protocol_error() -> None:
    ws = FakeWS([{"id": 1, "error": {"code": -32601, "message": "not found"}}])
    with pytest.raises(RuntimeError, match=r"CDP Network\.setCookie failed"):
        await sess.cdp_call(ws, itertools.count(1), "Network.setCookie", {})


async def test_inject_raises_when_chrome_unreachable() -> None:
    stack = await _LoopbackSessionStack.start()
    with pytest.raises(httpx.ConnectError):
        await sess.inject_session_cookie(_closed_port(), stack.gateway_url, SECRET)


# meiva:note
# gateway-session wiring


async def test_inject_once_skips_when_gateway_url_unset() -> None:
    stack = await _LoopbackSessionStack.start()
    keeper = GatewaySession(asyncio.TaskGroup(), stack.port, access=stack.access(gateway_url=""))
    await keeper.inject_once()  # must not raise
    assert stack.login_calls == []


async def test_inject_once_skips_on_empty_secret() -> None:
    stack = await _LoopbackSessionStack.start()
    access = GatewayAccess(
        gateway_url=stack.gateway_url,
        cluster_secret="",
        remote_plane=False,
        machine_token=None,
        launcher_profile=None,
        gateway_home=True,
    )
    keeper = GatewaySession(asyncio.TaskGroup(), stack.port, access=access)
    await keeper.inject_once()
    assert stack.login_calls == []


async def test_inject_once_injects_with_settings() -> None:
    stack = await _LoopbackSessionStack.start()
    keeper = GatewaySession(asyncio.TaskGroup(), stack.port, access=stack.access())
    await keeper.inject_once()
    assert len(stack.login_calls) == 1
    assert len(stack.page_sent) == 1


def test_session_params_present_the_delivered_runner_token() -> None:
    delivered = "delivered-runner-value"
    access = GatewayAccess(
        gateway_url=GATEWAY,
        cluster_secret=SECRET,
        remote_plane=False,
        machine_token=delivered,
        launcher_profile="runner",
        gateway_home=False,
    )
    assert gateway_session.resolve_gateway_session_params(access) == (
        GATEWAY,
        delivered,
    )


def test_session_params_without_a_token_log_the_defect_instead_of_using_the_secret(
    loguru_records: LogRecords,
) -> None:
    """The daemon keeps serving without the cookie (best effort), but a launch
    that delivered no token is an error, and the human secret never stands in."""
    access = GatewayAccess(
        gateway_url=GATEWAY,
        cluster_secret=SECRET,
        remote_plane=False,
        machine_token=None,
        launcher_profile="runner",
        gateway_home=False,
    )

    assert gateway_session.resolve_gateway_session_params(access) is None

    errors = loguru_records.messages("ERROR")
    assert len(errors) == 1
    assert "MEIVA_API_TOKEN" in errors[0]
    assert SECRET not in errors[0]


async def test_inject_once_swallows_injection_failure() -> None:
    stack = await _LoopbackSessionStack.start(login_status=500)
    keeper = GatewaySession(asyncio.TaskGroup(), stack.port, access=stack.access())
    await keeper.inject_once()  # must not raise


async def test_session_loop_injects_then_waits() -> None:
    stack = await _LoopbackSessionStack.start()
    stop = asyncio.Event()
    stack.stop_on_login = stop
    keeper = GatewaySession(asyncio.TaskGroup(), stack.port, access=stack.access())
    await keeper.refresh_loop(stop)
    assert len(stack.login_calls) == 1


async def test_spawn_inject_tracks_task() -> None:
    stack = await _LoopbackSessionStack.start()
    async with asyncio.TaskGroup() as tasks:
        keeper = GatewaySession(tasks, stack.port, access=stack.access())
        keeper.spawn_inject()
        assert len(keeper.inject_tasks) == 1
        # let the task finish so its done-callback clears the set
        await asyncio.gather(*keeper.inject_tasks)
    assert keeper.inject_tasks == set()
    assert len(stack.login_calls) == 1


async def test_a_failing_one_shot_does_not_abort_its_group() -> None:
    stack = await _LoopbackSessionStack.start(login_status=500)
    async with asyncio.TaskGroup() as tasks:
        keeper = GatewaySession(tasks, stack.port, access=stack.access())
        keeper.spawn_inject()
        await asyncio.gather(*keeper.inject_tasks)
