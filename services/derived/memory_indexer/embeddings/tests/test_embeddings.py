"""Embedding provider contract tests — the abstraction's hard gate.

The Gemini adapter's wire behavior is pinned exactly as it was before the
abstraction (endpoint, payload shape, auth header, per-site retry
policies, dim, shape validation) — these tests ARE the statement that
"Gemini adapter behavior is unchanged". The HTTP call is mocked
(`httpx.AsyncClient`), with a dummy API key. Deadline guards additionally
use a local trickling HTTP server.

Also pins the factory switch: `MEIVA_EMBEDDING_BACKEND` dispatch, unknown
values fail fast, and the provider declares the vector space (`dim` +
`fingerprint`) the storage layer consumes.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import sys
import threading
import time
from collections.abc import AsyncGenerator, Generator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import Any, Self, override

import httpx
import numpy as np
import pytest

from base.host.proc import run_bounded
from base.lm.catalog import ModelCatalog
from base.lm.provider_api import ProviderBinding
from base.packages.plugin_providers import build_model_catalog
from base.paths import meiva_home
from services.derived.memory_indexer.embeddings import gemini
from services.derived.memory_indexer.embeddings.base import EmbeddingAPIError
from services.derived.memory_indexer.embeddings.gemini import (
    DIM,
    EMBED_POLICY,
    ENDPOINT,
    MODEL_ID,
    QUERY_EMBED_POLICY,
    GeminiEmbeddingProvider,
)

# Retry backoff waits are recorded, not slept, so retry-path tests do not hang; the retry loop
# itself is still exercised (call counts). The provider's policy is a module constant (R2-D), no
# longer settings-driven. Tests that measure elapsed time on real sockets restore the real waits.


TIMEOUT_S = 60.0
API_KEY = "test-gemini-key"

pytestmark = pytest.mark.usefixtures("retry_waits")


def build_provider(
    *,
    catalog: ModelCatalog | None = None,
    timeout_s: float = TIMEOUT_S,
    api_key: str | None = API_KEY,
    endpoint: str = ENDPOINT,
    transport: httpx.AsyncBaseTransport | None = None,
) -> GeminiEmbeddingProvider:
    active_catalog = build_model_catalog(home=meiva_home()) if catalog is None else catalog
    return GeminiEmbeddingProvider(
        active_catalog,
        timeout_s=timeout_s,
        api_key=api_key,
        endpoint=endpoint,
        transport=transport,
    )


_provider = build_provider


class _FakeResponse(httpx.Response):
    """Buffered response with the real HTTPX status contract."""

    def __init__(self, payload: dict[str, Any], *, status_code: int = 200) -> None:
        super().__init__(
            status_code,
            json=payload,
            request=httpx.Request("POST", ENDPOINT),
        )


class AsyncClient(httpx.AsyncBaseTransport):
    """Records attempts across client instances, including sync retry loops."""

    def __init__(
        self,
        vectors: list[list[float]] | None = None,
        *,
        raises_times: int = 0,
        status_code: int = 200,
        prompt_token_count: int | None = None,
        response_body: dict[str, Any] | None = None,
        enter_error: Exception | None = None,
        request_error: Exception | None = None,
        stage_calls: list[str] | None = None,
    ) -> None:
        self._vectors = vectors or []
        self._raises_remaining = raises_times
        self._status_code = status_code
        self._prompt_token_count = prompt_token_count
        self._response_body = response_body
        self._enter_error = enter_error
        self._request_error = request_error
        self._stage_calls = stage_calls
        self.call_count = 0
        self.calls: list[dict[str, Any]] = []
        self.timeout = 0.0

    def __call__(self, *, timeout: float) -> AsyncClient:
        self.timeout = timeout
        return self

    @override
    async def __aenter__(self) -> Self:
        if self._stage_calls is not None and self._enter_error is not None:
            self._stage_calls.append("client")
        if self._enter_error is not None:
            raise self._enter_error
        return self

    @override
    async def __aexit__(self, *args: object) -> None:
        return None

    def _build_response(
        self, url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> httpx.Response:
        if self._stage_calls is not None and self._request_error is not None:
            self._stage_calls.append("request")
        if self._request_error is not None:
            raise self._request_error
        self.call_count += 1
        self.timeout = timeout
        self.calls.append({"url": url, "json": payload, "headers": headers, "timeout": timeout})
        if self._raises_remaining > 0:
            self._raises_remaining -= 1
            raise httpx.ConnectError("simulated network failure")
        body: dict[str, Any] = {"embeddings": [{"values": v} for v in self._vectors]}
        if self._prompt_token_count is not None:
            body["usageMetadata"] = {"promptTokenCount": self._prompt_token_count}
        if self._response_body is not None:
            body = self._response_body
        return _FakeResponse(body, status_code=self._status_code)

    @override
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        timeout_ext: Mapping[str, float] | None = request.extensions.get("timeout")
        timeout = (
            float(timeout_ext["read"])
            if timeout_ext is not None and "read" in timeout_ext
            else self.timeout
        )
        payload: dict[str, Any] = json.loads(request.content.decode("utf-8"))
        return self._build_response(str(request.url), payload, dict(request.headers), timeout)

    @asynccontextmanager
    async def stream(
        self, method: str, url: str, *, json: dict[str, Any], headers: dict[str, str]
    ) -> AsyncGenerator[httpx.Response]:
        assert method == "POST"
        yield self._build_response(url, json, headers, self.timeout)


class _ObservedBindings(Mapping[str, ProviderBinding]):
    def __init__(
        self,
        inner: Mapping[str, ProviderBinding],
        *,
        seen: list[Mapping[str, ProviderBinding]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._inner = inner
        self._seen = seen
        self._error = error

    @override
    def __getitem__(self, key: str) -> ProviderBinding:
        return self._inner[key]

    @override
    def __iter__(self) -> Iterator[str]:
        if self._seen is not None:
            self._seen.append(self)
        if self._error is not None:
            raise self._error
        return iter(self._inner)

    @override
    def __len__(self) -> int:
        return len(self._inner)


# ── provider surface (the contract) ───────────────────────────────────────


def test_provider_declares_vector_space() -> None:
    """dim + fingerprint are the provider's declaration of its vector space
    — the storage layer's schema width and the reconcile key."""
    provider = _provider()
    assert provider.name == "gemini"
    assert provider.dim == DIM == 3072
    assert provider.fingerprint == f"gemini:{MODEL_ID}:dim=3072"
    assert provider.fingerprint.startswith("gemini:")


def test_embed_batch_shape() -> None:
    fake = AsyncClient(vectors=[[1.0] * DIM, [2.0] * DIM])
    result = _provider(transport=fake).embed_batch(["text1", "text2"])
    assert result.shape == (2, DIM)
    assert result.dtype == np.float32
    body = fake.calls[-1]["json"]
    assert [r["taskType"] for r in body["requests"]] == ["RETRIEVAL_DOCUMENT"] * 2


def test_embed_query_shape() -> None:
    fake = AsyncClient(vectors=[[1.0] * DIM])
    result = _provider(transport=fake).embed_query("hello")
    assert result.shape == (DIM,)
    body = fake.calls[-1]["json"]
    assert body["requests"][0]["taskType"] == "RETRIEVAL_QUERY"


def test_embed_batch_emits_priced_billing_span(tmp_path: Path) -> None:
    result = run_bounded(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("billing_child.py")),
            "priced",
            str(tmp_path / "requests"),
        ],
        env={
            "HOME": str(tmp_path),
            "MEIVA_HOME": str(tmp_path),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_CONFIG_BOOT": "eager",
            "MEIVA_PROCESS_PROFILE": "agent",
        },
        timeout=90,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_embed_batch_unpriced_catalog_emits_unpriced_billing_span(tmp_path: Path) -> None:
    result = run_bounded(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("billing_child.py")),
            "unpriced",
            str(tmp_path / "requests"),
        ],
        env={
            "HOME": str(tmp_path),
            "MEIVA_HOME": str(tmp_path),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_CONFIG_BOOT": "eager",
            "MEIVA_PROCESS_PROFILE": "agent",
        },
        timeout=90,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_embed_batch_emits_priced_accounting_without_usage_metadata(tmp_path: Path) -> None:
    result = run_bounded(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("billing_child.py")),
            "missing",
            str(tmp_path / "requests"),
        ],
        env={
            "HOME": str(tmp_path),
            "MEIVA_HOME": str(tmp_path),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_CONFIG_BOOT": "eager",
            "MEIVA_PROCESS_PROFILE": "agent",
        },
        timeout=90,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_embed_query_async_shape() -> None:
    """Async queries use the same client seam and query payload as sync queries."""
    fake = AsyncClient(vectors=[[1.0] * DIM])
    result = asyncio.run(_provider(transport=fake).embed_query_async("hello"))
    assert result.shape == (DIM,)
    call = fake.calls[-1]
    assert call["url"] == ENDPOINT
    assert call["headers"]["x-goog-api-key"]
    body = call["json"]
    assert body["requests"][0]["taskType"] == "RETRIEVAL_QUERY"
    assert body["requests"][0]["outputDimensionality"] == DIM


def test_embed_query_async_emits_priced_billing_span(tmp_path: Path) -> None:
    result = run_bounded(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("billing_child.py")),
            "async",
            str(tmp_path / "requests"),
        ],
        env={
            "HOME": str(tmp_path),
            "MEIVA_HOME": str(tmp_path),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_CONFIG_BOOT": "eager",
            "MEIVA_PROCESS_PROFILE": "agent",
        },
        timeout=90,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_embed_request_payload_and_auth() -> None:
    """Wire contract: endpoint, api-key header, per-text request shape."""
    fake = AsyncClient(vectors=[[1.0] * DIM])
    _provider(transport=fake).embed_batch(["hello world"])
    call = fake.calls[-1]
    assert call["url"] == ENDPOINT
    assert call["headers"]["x-goog-api-key"]  # non-empty key forwarded
    req = call["json"]["requests"][0]
    assert req["model"] == f"models/{MODEL_ID}"
    assert req["outputDimensionality"] == DIM
    assert req["content"]["parts"][0]["text"] == "hello world"


def test_embed_batch_empty_short_circuit(tmp_path: Path) -> None:
    result = run_bounded(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("billing_child.py")),
            "empty",
            str(tmp_path / "requests"),
        ],
        env={
            "HOME": str(tmp_path),
            "MEIVA_HOME": str(tmp_path),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_CONFIG_BOOT": "eager",
            "MEIVA_PROCESS_PROFILE": "agent",
        },
        timeout=90,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_embed_retries_then_succeeds(tmp_path: Path) -> None:
    result = run_bounded(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("billing_child.py")),
            "retry",
            str(tmp_path / "requests"),
        ],
        env={
            "HOME": str(tmp_path),
            "MEIVA_HOME": str(tmp_path),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_CONFIG_BOOT": "eager",
            "MEIVA_PROCESS_PROFILE": "agent",
        },
        timeout=90,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_embed_raises_after_max_retries(tmp_path: Path) -> None:
    result = run_bounded(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("billing_child.py")),
            "failed",
            str(tmp_path / "requests"),
        ],
        env={
            "HOME": str(tmp_path),
            "MEIVA_HOME": str(tmp_path),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_CONFIG_BOOT": "eager",
            "MEIVA_PROCESS_PROFILE": "agent",
        },
        timeout=90,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("mode", ["batch", "query", "async_query"])
@pytest.mark.parametrize("error_type", [RuntimeError, TypeError])
def test_embed_usage_failure_propagates(mode: str, error_type: type[Exception]) -> None:
    """Accounting fails after one successful provider call, without replaying it."""
    fake = AsyncClient(vectors=[[1.0] * DIM], prompt_token_count=123)
    failure = error_type("billing exploded")
    base_catalog = build_model_catalog(home=meiva_home())
    catalog = replace(
        base_catalog,
        bindings=_ObservedBindings(base_catalog.bindings, error=failure),
    )
    provider = _provider(catalog=catalog, transport=fake)
    with pytest.raises(error_type) as caught:
        if mode == "batch":
            provider.embed_batch(["hello"])
        elif mode == "query":
            provider.embed_query("hello")
        else:
            asyncio.run(provider.embed_query_async("hello"))
    assert caught.value is failure
    assert len(fake.calls) == 1


def test_factory_passes_same_catalog_to_all_embedding_paths() -> None:
    from services.derived.memory_indexer.embeddings import factory

    base_catalog = build_model_catalog(home=meiva_home())
    seen: list[Mapping[str, ProviderBinding]] = []
    bindings = _ObservedBindings(base_catalog.bindings, seen=seen)
    catalog = replace(base_catalog, bindings=bindings)
    fake = AsyncClient(vectors=[[1.0] * DIM], prompt_token_count=123)
    provider = factory.get_provider(
        "gemini", catalog=catalog, timeout_s=TIMEOUT_S, api_key=API_KEY, transport=fake
    )
    provider.embed_batch(["hello"])
    provider.embed_query("hello")
    asyncio.run(provider.embed_query_async("hello"))
    assert len(seen) == 3
    assert all(received is bindings for received in seen)
    assert len(fake.calls) == 3


def test_embed_http_error_status_retries_then_raises() -> None:
    """Non-2xx (raise_for_status) is a retryable failure; exhausting retries raises."""
    fake = AsyncClient(vectors=[[1.0] * DIM], status_code=500)
    with pytest.raises(EmbeddingAPIError, match="failed after"):
        _provider(transport=fake).embed_query("hello")

    assert len(fake.calls) == QUERY_EMBED_POLICY.max_attempts


def test_query_embed_policy_is_lighter_than_document_policy() -> None:
    """Query embeds and indexer document embeds answer to different masters
    (task #2003/B): a search query sits inside the gateway's own search
    deadline, so the indexer's 4-attempt schedule (1->2->4->8s) could spend
    the whole budget retrying
    a 429 the caller (passive recall / an explicit meiva.memory.search) would
    simply watch expire — and the caller retries the *search*, not the embed."""
    assert QUERY_EMBED_POLICY.max_attempts < EMBED_POLICY.max_attempts


def test_embed_query_async_uses_the_query_policy() -> None:
    """The gateway's async query embed retries on the lighter query schedule
    (2 attempts), not the indexer's 4: a 429 during a fleet wake must not burn
    the search deadline on retries."""

    fake = AsyncClient(raises_times=100)
    with pytest.raises(EmbeddingAPIError, match="failed after 2 attempts"):
        asyncio.run(_provider(transport=fake).embed_query_async("hello"))
    assert len(fake.calls) == QUERY_EMBED_POLICY.max_attempts


def test_embed_4xx_fails_immediately() -> None:
    """A deterministic 4xx is permanent: one attempt, then EmbeddingAPIError
    (R2-D classify; the pre-R2 loop wasted its whole budget retrying 400/403 —
    audit 06 Q4)."""

    fake = AsyncClient(status_code=400)
    with pytest.raises(EmbeddingAPIError, match="Gemini embed HTTP 400"):
        _provider(transport=fake).embed_query("hello")
    assert len(fake.calls) == 1  # 4xx -> permanent -> single attempt


# meiva:note
# The per-request timeout is the value the service root read from config
# (MEIVA_EMBED_TIMEOUT_SECONDS, task #698 G8); the retry policy is a module
# constant (R2-D).
def test_embed_timeout_is_the_value_the_provider_was_given() -> None:
    fake = AsyncClient(vectors=[[1.0] * DIM], raises_times=1)

    _provider(timeout_s=12.5, transport=fake).embed_batch(["hello"])

    # meiva:note
    # 1 failure + 1 retry
    assert len(fake.calls) == 2
    for call in fake.calls:
        assert call["timeout"] == 12.5


# meiva:note
# Missing key -> EmbeddingAPIError with actionable guidance, before any POST.
def test_embed_no_api_key_raises() -> None:
    # No key must mean no network attempt — this makes that observable.
    fake = AsyncClient(enter_error=AssertionError("must not enter client without an API key"))
    with pytest.raises(EmbeddingAPIError) as exc_info:
        _provider(api_key=None, transport=fake).embed_query("hello")
    message = str(exc_info.value)
    assert "GEMINI_API_KEY" in message
    assert ".env" in message  # actionable: points the operator at where to set it
    assert fake.calls == []


def test_embed_async_client_construction_failure_wraps() -> None:
    """A modelled client transport failure becomes EmbeddingAPIError (#971).

    Client construction remains outside the async request retry loop.
    """
    calls: list[str] = []
    fake = AsyncClient(
        enter_error=httpx.ConnectError("client init failed"),
        stage_calls=calls,
    )
    with pytest.raises(EmbeddingAPIError, match="client init failed"):
        asyncio.run(_provider(transport=fake).embed_query_async("hello"))
    assert calls == ["client"]


@pytest.mark.parametrize("mode", ["batch", "query", "async_query"])
@pytest.mark.parametrize("phase", ["client", "request"])
@pytest.mark.parametrize("error_type", [TypeError, ValueError, RuntimeError])
def test_unknown_http_stage_failure_propagates_without_retry(
    mode: str, phase: str, error_type: type[Exception]
) -> None:
    failure = error_type("local invariant failed")
    calls: list[str] = []
    fake = (
        AsyncClient(enter_error=failure, stage_calls=calls)
        if phase == "client"
        else AsyncClient(request_error=failure, stage_calls=calls)
    )
    provider = _provider(transport=fake)
    with pytest.raises(error_type) as caught:
        if mode == "batch":
            provider.embed_batch(["hello"])
        elif mode == "query":
            provider.embed_query("hello")
        else:
            asyncio.run(provider.embed_query_async("hello"))
    assert caught.value is failure
    assert calls == [phase]


def test_embed_response_shape_mismatch_raises() -> None:
    fake = AsyncClient(vectors=[[1.0] * DIM] * 3)
    with pytest.raises(EmbeddingAPIError, match="unexpected shape"):
        _provider(transport=fake).embed_batch(["a", "b"])


def test_embed_malformed_response_raises() -> None:
    """Embedding entries missing `values` -> EmbeddingAPIError, not a crash."""

    fake = AsyncClient(response_body={"embeddings": [{"nope": []}]})
    with pytest.raises(EmbeddingAPIError, match="malformed"):
        _provider(transport=fake).embed_query("hello")


def test_sync_embed_rejects_running_loop() -> None:
    """Fail with caller guidance before creating a coroutine or a client."""
    fake = AsyncClient(
        enter_error=AssertionError("running-loop guard must precede client construction")
    )

    async def invoke() -> None:
        with pytest.raises(RuntimeError, match="sync embedding provider API") as error:
            gemini.embed(
                ["hello"],
                "RETRIEVAL_DOCUMENT",
                catalog=build_model_catalog(home=meiva_home()),
                timeout_s=TIMEOUT_S,
                api_key=API_KEY,
                transport=fake,
            )
        assert type(error.value) is RuntimeError
        assert error.value.__cause__ is None
        message = str(error.value)
        assert "asyncio.to_thread or an executor" in message
        assert "embed_query_async" in message
        assert "batch embedding stays sync" in message
        assert fake.calls == []

    asyncio.run(invoke())


# ── factory switch ────────────────────────────────────────────────────────


@contextmanager
def embedding_server(
    *,
    compressed: bool = False,
    chunked: bool = False,
    drip: bool = True,
    framing: str | None = None,
    framing_started: threading.Event | None = None,
    error_status: int | None = None,
) -> Generator[tuple[str, list[float]]]:
    """Drip body or HTTP framing while every read gap stays below the timeout."""
    if framing is not None:
        chunked = True
    started: list[float] = []
    stop = threading.Event()
    body = json.dumps({"embeddings": [{"values": [1.0] * DIM}]}).encode()
    if compressed:
        buffer = BytesIO()
        with gzip.GzipFile(filename="x" * 80, mode="wb", fileobj=buffer, mtime=0) as member:
            member.write(body)
        body = buffer.getvalue()
        # Raw reads cover the long gzip filename without producing decoded bytes.
        prefix_size, read_gap = 90, 0.025
    else:
        body = b" " * 40 + body
        prefix_size, read_gap = 40, 0.05
    chunks = (
        [body[i : i + 1] for i in range(prefix_size)] + [body[prefix_size:]]
        if drip
        else [body[i : i + 11] for i in range(0, len(body), 11)]
    )

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _reject(self, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Length", "40")
            self.send_header("Retry-After", "7")
            self.end_headers()
            try:
                for _ in range(40):
                    self.wfile.write(b"e")
                    self.wfile.flush()
                    if stop.wait(0.05):
                        return
            except (BrokenPipeError, ConnectionResetError):
                return  # Header-phase rejection closes the unread error body.

        def _write_framing_fragments(self, framing: str) -> None:
            size = f"{len(body):x}".encode()
            if framing == "extension":
                wire = [size + b";name="] + [b"x"] * 40
                wire.append(b"\r\n" + body + b"\r\n0\r\n\r\n")
            elif framing == "trailer":
                wire = [size + b"\r\n" + body + b"\r\n0\r\nX-Test: "]
                wire += [b"y"] * 40 + [b"\r\n\r\n"]
            else:
                raise AssertionError(f"unknown framing: {framing}")
            for fragment in wire:
                self.wfile.write(fragment)
                self.wfile.flush()
                if framing_started is not None:
                    framing_started.set()
                if stop.wait(read_gap):
                    return

        def _write_body_chunks(self) -> None:
            for chunk in chunks:
                if chunked:
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
                if drip and stop.wait(read_gap):
                    return
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            started.append(time.monotonic())
            if error_status is not None and len(started) == 1:
                self._reject(error_status)
                return
            self.send_response(200)
            if compressed:
                self.send_header("Content-Encoding", "gzip")
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                if framing is not None:
                    self._write_framing_fragments(framing)
                else:
                    self._write_body_chunks()
            except (BrokenPipeError, ConnectionResetError):
                return  # Expected when the client cancels the attempt.

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/embed", started
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.fixture
def trickle_server() -> Iterator[tuple[str, list[float]]]:
    with embedding_server() as server:
        yield server
