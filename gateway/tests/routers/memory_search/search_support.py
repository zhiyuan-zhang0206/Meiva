# meiva:note
# A memory search against real parts.
#
# The vector index is a service on a port of its own that answers `POST /search` the way
# `services/derived/memory_search` does; the notes are files on disk; the gateway app is the
# real one. Two outbound calls reach paid provider endpoints that are module constants, so each
# is one fake: the embedding of the query and the decision model's relevance verdict. The
# embedder is the app's `memory_embedder`; the verdict is installed by the `rig` fixture; the
# tests steer both through plain attributes.

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import numpy as np
import pytest
from fastapi import FastAPI

from base.cluster.machine import MachineIdentityReader
from base.host.db_authority.api import AcceptanceCache
from base.lm.catalog import ModelCatalog
from base.lm.catalog.decide.types import (
    FALSE,
    TRUE,
    Decision,
    DecisionBuildContext,
    DecisionResult,
    Question,
)
from base.lm.catalog.provider_keys import ProviderKeys
from gateway.http.config import GatewayConfig
from gateway.routers.memory_api import memory as memory_router
from gateway.tests.agents.gateway_auth import gateway_credentials
from gateway.tests.auth.gateway_config import (
    GatewayTestClient,
    gateway_test_app,
    gateway_test_client,
    gateway_test_config,
)
from services.derived.memory_indexer.backends.base import ChunkHit
from services.derived.memory_indexer.embeddings.base import EmbeddingProvider

__all__ = [
    "SEARCH_PERMITS",
    "FakeEmbedder",
    "Judge",
    "SearchRig",
    "SearchService",
    "asgi_client",
    "assert_semaphore_locked",
    "hit",
    "memory_config",
    "post_memory_search",
    "rig",
    "stalled_search",
]


def hit(path: Path, idx: int = 0, kind: str = "body") -> ChunkHit:
    return ChunkHit(str(path), kind, idx, 0.5)


# meiva:note
# The test gateway's HTTP policy with the memory pool at `root` and configuration `fields` applied.
def memory_config(root: Path, **fields: Any) -> GatewayConfig:
    return gateway_test_config(fields, memory_root=root)


# meiva:note
# The vector service: answers `POST /search` with `hits`, the first `k` of them.
#
# `mode` makes it fail (HTTP 500) or accept the request and never answer until `close`.
class SearchService:
    def __init__(self) -> None:
        self.hits: list[ChunkHit] = []
        self.asked: list[int] = []
        self.mode: Literal["answer", "fail", "hang"] = "answer"
        self._released = threading.Event()
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                request = cast(dict[str, Any], json.loads(self.rfile.read(length)))
                service.asked.append(int(request["k"]))
                if service.mode == "hang":
                    service._released.wait(timeout=30)
                    return
                if service.mode == "fail":
                    self.send_response(500)
                    self.end_headers()
                    return
                data = json.dumps(
                    {
                        "hits": [
                            {
                                "path": h.path,
                                "kind": h.kind,
                                "chunk_idx": h.chunk_idx,
                                "score": h.score,
                            }
                            for h in service.hits[: request["k"]]
                        ]
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._released.set()
        self._server.shutdown()
        self._server.server_close()


# meiva:note
# The query embedding: a zero vector, after `delay`, or the `failure` it is told to raise.
#
# Retained as a fake because the Gemini endpoint is a module constant of the provider.
class FakeEmbedder:
    dim = 768
    fingerprint = "fake:provider:dim=768"

    def __init__(self) -> None:
        self.failure: Exception | None = None
        self.delay_s = 0.0
        self.hang = False
        self.in_flight = threading.Event()
        self.real = False

    async def embed_query_async(self, _text: str) -> np.ndarray:
        self.in_flight.set()
        if self.hang:
            await asyncio.Event().wait()
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.failure is not None:
            raise self.failure
        return np.zeros(self.dim, dtype=np.float32)


# meiva:note
# The decision model: probability by a marker in the question, default 0.5.
#
# Records every call so tests can assert what the endpoint asked and with which model.
# `blocking` makes every call wait until cancelled (`started`, `cancelled` count them).
class Judge:
    def __init__(self) -> None:
        self.rules: dict[str, float] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.failure: Exception | None = None
        self.blocking = False
        self.expected_started = 0
        self.started = 0
        self.cancelled = 0
        self.all_started = asyncio.Event()

    async def __call__(
        self,
        model: str,
        state: str,
        question: Any,
        *,
        catalog: ModelCatalog,
        keys: ProviderKeys,
        usage_kind: str,
    ) -> float:
        del catalog, keys, usage_kind
        self.calls.append((model, state, question))
        if self.failure is not None:
            raise self.failure
        if self.blocking:
            self.started += 1
            if self.started >= self.expected_started:
                self.all_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        return next((p for marker, p in self.rules.items() if marker in state), 0.5)


class _JudgeBinding:
    def __init__(self, judge: Judge, ctx: DecisionBuildContext, catalog: ModelCatalog) -> None:
        self._judge = judge
        self._ctx = ctx
        self._catalog = catalog

    async def decide(self, state: str, questions: Mapping[str, Question]) -> DecisionResult:
        question = questions["relevant"]
        prob = await self._judge(
            self._ctx.model,
            state,
            question,
            catalog=self._catalog,
            keys=self._ctx.keys,
            usage_kind="memory_rerank",
        )
        return DecisionResult(
            decisions={
                "relevant": Decision(
                    top=TRUE if prob >= 0.5 else FALSE,
                    probabilities={TRUE: prob, FALSE: round(1.0 - prob, 6)},
                )
            },
            input_tokens=10,
        )


def _catalog_with_judge(catalog: ModelCatalog, judge: Judge) -> ModelCatalog:
    def build(ctx: DecisionBuildContext) -> _JudgeBinding:
        return _JudgeBinding(judge, ctx, catalog)

    providers = {
        prefix: replace(
            provider,
            key_env="MEIVA_HOME",
            build=build,
        )
        for prefix, provider in catalog.decision_providers.items()
    }
    return replace(catalog, decision_providers=providers)


# meiva:note
# The service, the two fakes and the configuration every app built by the rig carries.
@dataclass
class SearchRig:
    service: SearchService
    judge: Judge
    embedder: FakeEmbedder
    machine: MachineIdentityReader
    catalog: ModelCatalog
    fields: dict[str, Any] = field(default_factory=dict)

    # meiva:note
    # Set config fields (`memory_rerank_candidates=40`) for the apps and clients built after.
    def configure(self, **fields: Any) -> None:
        self.fields.update(fields)

    # meiva:note
    # A running gateway whose memory pool is `root`, searching this rig's service.
    @contextmanager
    def client(self, root: Path | None = None) -> Generator[GatewayTestClient]:
        with gateway_test_client(
            config=None if root is None else memory_config(root, **self.fields),
            fields=self.fields,
        ) as client:
            client.app.state.catalog = self.catalog
            client.app.state.memory_embedder = self.embedder_for(client.app.state.gateway_config)
            yield client

    # meiva:note
    # The embedder a search uses: this rig's fake, or, once `real` is set, the real provider
    # built from the app's own config and catalog.
    def embedder_for(self, config: GatewayConfig) -> EmbeddingProvider:
        if self.embedder.real:
            return memory_router.build_embedder(config.memory_search, self.catalog)
        return cast(EmbeddingProvider, self.embedder)

    # meiva:note
    # The gateway app without its lifespan (an ASGI transport does not run one), with the
    # pieces a search reads set from the real process: database handle, catalog, a fresh gate
    # (a semaphore binds to the loop of its first contended acquire, and each async test
    # has its own).
    def asgi_app(self, root: Path, *, permits: int) -> FastAPI:
        app = gateway_test_app(
            machine=self.machine, config=memory_config(root, **self.fields), fields=self.fields
        )
        acceptance: AcceptanceCache = {}
        app.state.machine_token_acceptance = acceptance
        app.state.db = app.state.gateway_process.database()
        app.state.catalog = self.catalog
        app.state.memory_embedder = self.embedder_for(app.state.gateway_config)
        app.state.machine = self.machine
        app.state.memory_search_gate = asyncio.Semaphore(permits)
        return app


# meiva:note
# Drive `app` on the *test's* own event loop, so concurrent requests contend for the
# handler's semaphore the way they do in the gateway. `TestClient` would run them on its own
# portal loop instead.
def asgi_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers=gateway_credentials(app.state.gateway_config),
    )


@pytest.fixture
def rig(
    model_catalog: ModelCatalog,
    machine_identity: MachineIdentityReader,
) -> Generator[SearchRig]:
    service = SearchService()
    judge = Judge()
    embedder = FakeEmbedder()
    try:
        yield SearchRig(
            service=service,
            judge=judge,
            embedder=embedder,
            machine=machine_identity,
            catalog=_catalog_with_judge(model_catalog, judge),
            fields={"memory_search_uri": service.url, "memory_search_backend": "numpy"},
        )
    finally:
        service.close()


# meiva:note
# the per-test semaphore size of a stalled-search app
SEARCH_PERMITS = 2
_TEST_DEADLINE_S = 0.5


# meiva:note
# An app whose vector service accepts a search and never answers, under a short deadline
# (or the config `fields` given).
#
# The returned app's `memory_search_gate` is the fresh per-test semaphore.
def stalled_search(
    rig: SearchRig, root: Path, *, permits: int = SEARCH_PERMITS, **fields: Any
) -> FastAPI:
    (root / "a.md").write_text("---\ntype: Memory\n---\nx\n", encoding="utf-8")
    rig.configure(memory_search_deadline_seconds=_TEST_DEADLINE_S)
    rig.configure(**fields)
    rig.service.mode = "hang"
    rig.service.hits = [hit(root / "a.md")]
    return rig.asgi_app(root, permits=permits)


async def post_memory_search(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post("/api/memory/search", json={"query": "x", "k": 1, "purpose": "search"})


# meiva:note
# Wait for concurrent requests to acquire every memory-search permit.
async def assert_semaphore_locked(sem: asyncio.Semaphore, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not sem.locked() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert sem.locked(), "search holders never acquired every permit"
