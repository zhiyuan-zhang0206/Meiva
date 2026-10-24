# meiva:note
# Fresh-process scenarios for the gateway bootstrap tests.
#
# Usage: `child_gateway_scenarios.py <scenario> [args]`.
#
# - `checkout_move <source> <repo>`: import the gateway's first-load image from a
#   throwaway checkout, move that checkout's HEAD, and print what the image reports.
# - `server_shutdown`: run the real gateway server child for the SIGTERM tests.
# - `server_main`: run the real `gateway.cluster.server.main` (the process root) with a probe app;
#   the app builder leaves a marker file where `BUILT_ENV` names it, so a parent can tell whether
#   the root built the app itself.
# - `app_from_environment`: build the gateway app twice from the environment, as a reload worker does.

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

# meiva:note
# the route annotation is resolved from module globals
from starlette.requests import Request

if TYPE_CHECKING:
    from fastapi import FastAPI

MARKERS_ENV = "GATEWAY_SHUTDOWN_TEST_MARKERS"
PORT_ENV = "GATEWAY_SHUTDOWN_TEST_PORT"
REDIS_ENV = "GATEWAY_SHUTDOWN_TEST_REDIS"
BUILT_ENV = "GATEWAY_ROOT_TEST_BUILT"


def _mark(name: str) -> None:
    with Path(os.environ[MARKERS_ENV]).open("a", encoding="utf-8") as fh:
        fh.write(f"{name}\n")


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    _mark("lifespan-startup")
    try:
        yield
    finally:
        _mark("lifespan-shutdown")


def build_app() -> FastAPI:
    from fastapi import FastAPI
    from starlette.responses import PlainTextResponse, StreamingResponse

    from gateway.http.middleware import stopping

    app = FastAPI(lifespan=_lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/stream")
    async def _stream() -> StreamingResponse:
        async def _frames() -> AsyncIterator[bytes]:
            _mark("stream-open")
            try:
                while True:
                    yield b"data: tick\n\n"
                    await asyncio.sleep(0.1)
            except (asyncio.CancelledError, GeneratorExit) as exc:
                _mark(f"stream-closed:{type(exc).__name__}")
                raise

        return StreamingResponse(_frames(), media_type="text/event-stream")

    @app.get("/cooperative-stream")
    async def _cooperative_stream() -> StreamingResponse:
        async def _frames() -> AsyncIterator[bytes]:
            _mark("cooperative-stream-open")
            while not stopping.is_stopping():
                yield b"data: tick\n\n"
                await asyncio.sleep(0.1)
            _mark("cooperative-stream-ended-itself")

        return StreamingResponse(_frames(), media_type="text/event-stream")

    @app.get("/sse/{mode}")
    async def _sse(mode: str, request: Request) -> StreamingResponse:
        from base.events.live.bus import EventBus, EventBusConfig
        from gateway.events import sse
        from gateway.events.sse_frames import SseCadence

        bus = EventBus(
            EventBusConfig(redis_url=os.environ[REDIS_ENV], events_channel="gateway-shutdown-test")
        )
        if mode == "filtered":
            frames = sse.event_stream(bus, 7, request, poll_s=0.5, cadence=SseCadence())
        else:
            frames = sse.throttled_event_stream(
                bus, request, throttle_rate=20.0, cadence=SseCadence()
            )

        async def _marked() -> AsyncIterator[bytes]:
            async for frame in frames:
                _mark(f"sse-frame:{frame[:20]!r}")
                yield frame
            _mark("sse-ended-itself")

        return StreamingResponse(_marked(), media_type="text/event-stream")

    @app.get("/slow")
    async def _slow(seconds: float = 1.0) -> PlainTextResponse:
        _mark("slow-started")
        await asyncio.sleep(seconds)
        _mark("slow-finished")
        return PlainTextResponse("slow-done")

    return app


# meiva:note
# The app a `server_main` child serves: one route that says the process is up.
def probe_app() -> FastAPI:
    from fastapi import FastAPI

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/probe")
    def _probe() -> dict[str, int]:
        return {"pid": os.getpid()}

    return app


def _build_marked_probe_app(_process: object) -> FastAPI:
    Path(os.environ[BUILT_ENV]).write_text("built", encoding="utf-8")
    return probe_app()


def run_main() -> None:
    from base.config import ConfigBoot
    from gateway.cluster import server as _server

    config = ConfigBoot()
    config.boot()
    config.ensure_eager()
    _server.main(config=config, build_app=_build_marked_probe_app)


def _app_from_environment() -> None:
    from base.config import ConfigBoot
    from gateway import app as gateway_app

    first = gateway_app.create_app_from_environment()
    second = gateway_app.create_app_from_environment()
    expected = gateway_app.gateway_config_from_boot(ConfigBoot())
    sys.stdout.write(
        json.dumps(
            {
                "distinct_apps": first is not second,
                "config_matches_boot": first.state.gateway_config == expected,
                "acceptance_empty": first.state.machine_token_acceptance == {},
                "acceptance_independent": first.state.machine_token_acceptance
                is not second.state.machine_token_acceptance,
            }
        )
        + "\n"
    )


def run_child() -> None:
    import uvicorn

    from base.config import ConfigBoot
    from gateway.cluster import server as _server

    kwargs = _server.serve_kwargs(
        host="127.0.0.1",
        app="gateway.tests.bootstrap.child_gateway_scenarios:build_app",
        factory=True,
        config=ConfigBoot(),
    )
    kwargs["port"] = int(os.environ[PORT_ENV])
    _server.GatewayServer(uvicorn.Config(**kwargs)).run()


def _checkout_move(source: str, repo: str) -> None:
    sys.path[:0] = [source, repo]
    from base.native_process.code_version import CodeVersion
    from gateway.cluster import process_boot

    loaded = process_boot.LOADED_IMAGE
    root = Path(source)
    first = loaded.sha
    (root / "moved").write_text("new checkout")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)  # noqa: S603
    subprocess.run(  # noqa: S603
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=entry-proof",
            "-c",
            "user.email=proof@example.invalid",
            "commit",
            "-qm",
            "second",
        ],
        check=True,
    )
    repeated = process_boot.LOADED_IMAGE
    current = subprocess.check_output(  # noqa: S603
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    )
    sys.stdout.write(
        json.dumps(
            {
                "first": first,
                "after": repeated.sha,
                "same": repeated is loaded,
                "current": current.strip(),
                "version": CodeVersion(loaded).get(),
            }
        )
        + "\n"
    )


if __name__ == "__main__":
    if sys.argv[1] == "checkout_move":
        _checkout_move(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "server_shutdown":
        run_child()
    elif sys.argv[1] == "server_main":
        run_main()
    elif sys.argv[1] == "app_from_environment":
        _app_from_environment()
    else:
        raise SystemExit(f"unknown scenario {sys.argv[1]!r}")
