from __future__ import annotations

import http.server
import socket
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn

from base.daemon.health import DaemonProbe, ProbeVerdict
from services.derived.embeddinggemma.app import create_app
from services.derived.embeddinggemma.probe import probe_embeddinggemma
from services.derived.embeddinggemma.tests.fakes import (
    MODEL,
    REVISION,
    FakeEngine,
    opener,
    server_config,
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def _serving_app(tmp_path: Path, **config: object) -> Generator[str]:
    app = create_app(server_config(tmp_path, **config), open_engine=opener(FakeEngine()))
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        assert thread.is_alive(), "the server thread ended before it started"
        threading.Event().wait(0.02)
    try:
        yield f"http://127.0.0.1:{port}/healthz"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@contextmanager
def _answering(status: int, body: bytes) -> Generator[str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            _ = self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/healthz"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _probe(
    url: str, *, model: str = MODEL, revision: str | None = REVISION, device: str = "cpu"
) -> DaemonProbe:
    return probe_embeddinggemma(
        url=url, model=model, revision=revision, device=device, timeout_s=1.0
    )


def test_the_real_server_is_alive_for_the_identity_it_was_launched_with(tmp_path: Path) -> None:
    with _serving_app(tmp_path, device="gpu") as url:
        assert _probe(url, device="gpu").alive


@pytest.mark.parametrize(
    ("expected", "named"),
    [
        ({"device": "cpu"}, "device"),
        ({"model": "/legacy/model.litertlm,cpu"}, "model"),
        ({"revision": "b" * 64}, "revision"),
    ],
)
def test_a_server_launched_with_another_identity_is_down(
    tmp_path: Path, expected: dict[str, str], named: str
) -> None:
    with _serving_app(tmp_path, device="gpu") as url:
        probe = _probe(
            url,
            device=expected.get("device", "gpu"),
            **{key: value for key, value in expected.items() if key != "device"},
        )
    assert probe.verdict is ProbeVerdict.DOWN
    assert named in probe.detail


def test_an_unpinned_revision_is_not_compared(tmp_path: Path) -> None:
    with _serving_app(tmp_path) as url:
        assert _probe(url, revision=None).alive


def test_the_unmanaged_server_that_reports_a_backend_and_no_device_is_down() -> None:
    legacy = b'{"status": "ok", "model": "embeddinggemma-2", "backend": "cpu"}'
    with _answering(200, legacy) as url:
        assert not _probe(url, revision=None).alive


@pytest.mark.parametrize("status", [404, 500, 503])
def test_a_non_2xx_answer_is_down(status: int) -> None:
    with _answering(status, b"{}") as url:
        assert not _probe(url).alive


@pytest.mark.parametrize("body", [b"", b"not json", b"[1, 2]", b"null"])
def test_a_body_that_is_not_a_json_object_is_down(body: bytes) -> None:
    with _answering(200, body) as url:
        assert not _probe(url).alive


def test_nothing_listening_is_down() -> None:
    assert _probe(f"http://127.0.0.1:{_free_port()}/healthz").verdict is ProbeVerdict.DOWN
