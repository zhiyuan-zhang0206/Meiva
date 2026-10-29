import asyncio
import contextlib
import json
import threading
from collections.abc import Generator
from http import HTTPStatus
from typing import Any

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from services.supervision.healthchecks import browser_reach as hc


class _CdpStub:
    def __init__(self, *, fetch_outcome: str = "ok", hidden_supported: bool = True) -> None:
        self.fetch_outcome = fetch_outcome
        self.hidden_supported = hidden_supported
        self.browser_messages: list[dict[str, Any]] = []
        self.page_messages: list[dict[str, Any]] = []
        self.port = 0
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._stop: asyncio.Future[None] | None = None
        self._release: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(timeout=5)

    def close(self) -> None:
        if self._release is not None:
            self._loop.call_soon_threadsafe(self._release.set)
        if self._stop is not None and not self._stop.done():
            self._loop.call_soon_threadsafe(self._stop.set_result, None)
        self._thread.join(timeout=5)
        self._loop.close()

    def _process_request(self, _connection: ServerConnection, request: Request) -> Response | None:
        if request.path == "/json/version":
            body = json.dumps(
                {"webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/browser/test"}
            ).encode()
            return Response(
                int(HTTPStatus.OK),
                "OK",
                Headers({"Content-Type": "application/json", "Content-Length": str(len(body))}),
                body,
            )
        return None

    async def _handle_ws(self, ws: ServerConnection) -> None:
        path = ws.request.path if ws.request is not None else ""
        if path.startswith("/devtools/browser/"):
            first_raw = await ws.recv()
            first = json.loads(first_raw)
            self.browser_messages.append(first)
            if not self.hidden_supported:
                await ws.send(
                    json.dumps(
                        {"id": first["id"], "error": {"message": "Hidden targets are unsupported"}}
                    )
                )
                return
            await ws.send(json.dumps({"id": first["id"], "result": {"targetId": "canary"}}))
            second_raw = await ws.recv()
            second = json.loads(second_raw)
            self.browser_messages.append(second)
            await ws.send(json.dumps({"id": second["id"], "result": {"success": True}}))
        elif path.startswith("/devtools/page/"):
            raw = await ws.recv()
            msg = json.loads(raw)
            self.page_messages.append(msg)
            if self.fetch_outcome == "timeout":
                await self._release.wait() if self._release is not None else asyncio.sleep(0)
                return
            await ws.send(
                json.dumps({"id": msg["id"], "result": {"result": {"value": self.fetch_outcome}}})
            )

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)

        async def _main() -> None:
            self._stop = self._loop.create_future()
            self._release = asyncio.Event()
            async with serve(
                self._handle_ws,
                "127.0.0.1",
                0,
                process_request=self._process_request,
            ) as server:
                sock: Any = next(iter(server.sockets))
                self.port = int(sock.getsockname()[1])
                self._ready.set()
                await self._stop

        self._loop.run_until_complete(_main())


@contextlib.contextmanager
def _cdp_stub(*, fetch_outcome: str = "ok", hidden_supported: bool = True) -> Generator[_CdpStub]:
    stub = _CdpStub(fetch_outcome=fetch_outcome, hidden_supported=hidden_supported)
    stub.start()
    try:
        yield stub
    finally:
        stub.close()


# meiva:note
# The canary makes two separate claims, so two separate tests carry them. This one is the protocol
# shape: one hidden target in the shared context, one fetch expression, and the target closed in
# finally. It reads no clock, so a loaded runner cannot turn it into a different verdict.
def test_canary_opens_a_hidden_target_in_the_shared_context_and_closes_it() -> None:
    with _cdp_stub(fetch_outcome="ok") as stub:
        assert hc.canary(stub.port, "https://example.invalid/health", 5.0).outcome == "ok"

        create, close = stub.browser_messages
        assert create["method"] == "Target.createTarget"
        assert create["params"]["hidden"] is True
        assert create["params"]["background"] is True
        assert "browserContextId" not in create["params"]
        assert close["method"] == "Target.closeTarget"
        assert close["params"]["targetId"] == "canary"
        (evaluate,) = stub.page_messages
        assert evaluate["method"] == "Runtime.evaluate"
        assert 'fetch("https://example.invalid/health"' in evaluate["params"]["expression"]


# meiva:note
# A rejected fetch is its own classification: the page answers `error:TypeError`, and the canary says
# `error` - never reachability, never a skip.
def test_canary_reports_a_rejected_fetch_as_error() -> None:
    with _cdp_stub(fetch_outcome="error:TypeError") as stub:
        result = hc.canary(stub.port, "https://example.invalid/health", 5.0)
    assert result.outcome == "error"
    assert "error:TypeError" in result.detail


# meiva:note
# The timeout classification on its own: the page never settles, so the round must end as `timeout`.
# The budget is deliberately generous because the connection steps spend from the same deadline: a
# budget tight enough to race them (0.4s on a loaded runner) raises `TimeoutError` before the fetch
# is even asked, and the canary reports `skip` - no verdict - instead of `timeout`. This test judges
# the classification, never the elapsed time; the stub answers only when the test closes it, so the
# page cannot settle first.
def test_canary_reports_timeout_when_the_page_never_settles() -> None:
    with _cdp_stub(fetch_outcome="timeout") as stub:
        result = hc.canary(stub.port, "https://example.invalid/health", 5.0)
    assert result.outcome == "timeout"
    assert "did not settle" in result.detail


def test_canary_rejected_hidden_target_never_falls_back_to_visible_tab() -> None:
    with _cdp_stub(hidden_supported=False) as stub:
        result = hc.canary(stub.port, "https://example.invalid/health", 1)

        assert result.outcome == "skip"
        assert "Hidden targets are unsupported" in result.detail
        (create,) = stub.browser_messages
        assert create["params"]["hidden"] is True
        assert stub.page_messages == []


def test_canary_unexpected_failure_is_not_reachability_success() -> None:
    with _cdp_stub() as stub:
        closed_port = stub.port

    assert hc.canary(closed_port, "https://example.invalid/", 0.2).outcome == "skip"
