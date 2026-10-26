# meiva:note
# `POST /api/memory/search` endpoint tests — primary direct embed + the configured backend;
# relative-path conversion; wire error propagation.
#
# The vector index is a real listener (`search_support.SearchService`), the notes are files on
# disk and the app is the real gateway; only the query embedding and the decision model are
# fakes (`search_support.rig`).

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import Request
from pydantic import SecretStr

from base.config import settings
from base.host.net.tests.local_http import serve_asgi
from base.lm.pricing import Rates
from gateway.routers.memory_api import memory
from gateway.schemas import MemorySearchRequest
from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.routers.memory_search.search_support import SearchRig, hit
from gateway.tests.routers.memory_search.search_support import rig as rig
from services.derived.memory_indexer.embeddings.base import EmbeddingAPIError
from services.derived.memory_indexer.embeddings.gemini import DIM, GeminiEmbeddingProvider


# meiva:note
# Primary node goes directly through embedder + the backend, returns relative paths.
class TestPrimaryPath:
    # meiva:note
    # Primary path: stub embedder + index → verify returned paths are relative to memory_root.
    def test_primary_returns_relative_paths(self, tmp_path: Path, rig: SearchRig) -> None:
        # meiva:note
        # create a few fake markdown files in tmp so relative_to doesn't raise
        (tmp_path / "notes").mkdir()
        (tmp_path / "notes" / "foo.md").write_text("x")
        (tmp_path / "bar.md").write_text("y")
        rig.service.hits = [hit(tmp_path / "notes" / "foo.md"), hit(tmp_path / "bar.md")]

        with rig.client(tmp_path) as client:
            resp = client.post(
                "/api/memory/search", json={"query": "test", "k": 5, "purpose": "search"}
            )
        assert resp.status_code == 200
        # meiva:note
        # must be relative paths relative to memory_root — must not contain tmp_path prefix
        body = resp.json()
        results = body["results"]
        assert set(body) == {"results"}
        assert [item["path"] for item in results] == ["notes/foo.md", "bar.md"]
        assert len(results) == 2
        assert results[0]["path"] == "notes/foo.md"
        assert results[1]["path"] == "bar.md"
        # meiva:note
        # description is empty because stub files have no YAML frontmatter
        assert results[0]["description"] == ""
        assert results[1]["description"] == ""
        # meiva:note
        # each result carries the matched chunk and its relevance score
        assert [(item["chunk"], item["score"]) for item in results] == [("x", 0.5), ("y", 0.5)]

    # meiva:note
    # When files have a YAML frontmatter description, results include it.
    def test_primary_returns_descriptions_from_frontmatter(
        self, tmp_path: Path, rig: SearchRig
    ) -> None:
        (tmp_path / "with_desc.md").write_text(
            """---
type: Memory
description: A note about the user's health
title: Health Overview
---

# Health
""",
            encoding="utf-8",
        )
        (tmp_path / "no_desc.md").write_text(
            """---
type: Memory
title: No Description
---

# No desc
""",
            encoding="utf-8",
        )
        (tmp_path / "no_frontmatter.md").write_text("# Just a heading\n\nNo YAML.")
        rig.service.hits = [
            hit(tmp_path / "with_desc.md"),
            hit(tmp_path / "no_desc.md"),
            hit(tmp_path / "no_frontmatter.md"),
        ]

        with rig.client(tmp_path) as client:
            resp = client.post(
                "/api/memory/search", json={"query": "test", "k": 3, "purpose": "search"}
            )
        assert resp.status_code == 200
        body = resp.json()
        results = body["results"]
        assert len(results) == 3
        assert results[0]["path"] == "with_desc.md"
        assert results[0]["description"] == "A note about the user's health"
        assert results[1]["path"] == "no_desc.md"
        assert results[1]["description"] == ""
        assert results[2]["path"] == "no_frontmatter.md"
        assert results[2]["description"] == ""
        assert set(body) == {"results"}

    # meiva:note
    # embedder API failure → IndexerUnavailable (wire 503).
    def test_primary_embedder_failure_raises_indexer_unavailable(self, rig: SearchRig) -> None:
        rig.embedder.failure = EmbeddingAPIError("gemini quota exhausted")

        with rig.client() as client:
            resp = client.post(
                "/api/memory/search", json={"query": "x", "k": 5, "purpose": "search"}
            )
        assert resp.status_code == 503
        body = resp.json()
        assert body["reason"] == "indexer_unavailable"
        assert "embed" in body["detail"]

    # meiva:note
    # An unmodelled failure keeps its identity instead of becoming an outage.
    def test_unexpected_embed_failure_propagates(self, rig: SearchRig) -> None:
        failure = FileNotFoundError(2, "No such file or directory")
        rig.embedder.failure = failure

        with rig.client() as client, pytest.raises(FileNotFoundError) as caught:
            client.post("/api/memory/search", json={"query": "x", "k": 5, "purpose": "search"})
        assert caught.value is failure

    # meiva:note
    # The vector service errors or refuses the connection → IndexerUnavailable (wire 503).
    @pytest.mark.parametrize("fault", ["server-error", "refused"])
    def test_primary_backend_failure_raises_indexer_unavailable(
        self, rig: SearchRig, fault: str
    ) -> None:
        if fault == "server-error":
            rig.service.mode = "fail"
        else:
            rig.configure(memory_search_uri="http://127.0.0.1:1")

        with rig.client() as client:
            resp = client.post(
                "/api/memory/search", json={"query": "x", "k": 5, "purpose": "search"}
            )
        assert resp.status_code == 503
        assert resp.json()["reason"] == "indexer_unavailable"


# meiva:note
# The second stage orders the vector candidates by the decision model's probability.
class TestRerank:
    def _pool(self, tmp_path: Path) -> None:
        for name in ("a", "b", "c"):
            (tmp_path / f"{name}.md").write_text(
                f"---\ntype: Memory\ndescription: about {name}\ntags:\n- type/reference\n---\n\npassage {name}\n",
                encoding="utf-8",
            )

    def test_results_follow_the_decision_probability_not_vector_order(
        self, tmp_path: Path, rig: SearchRig
    ) -> None:
        self._pool(tmp_path)
        rig.service.hits = [hit(tmp_path / n) for n in ("a.md", "b.md", "c.md")]
        rig.judge.rules = {"passage a": 0.1, "passage b": 0.6, "passage c": 0.95}

        with rig.client(tmp_path) as client:
            resp = client.post(
                "/api/memory/search", json={"query": "what about c", "k": 2, "purpose": "search"}
            )
            configured_model = (
                client.app.state.gateway_process.config.view.services.memory_rerank_model
            )

        assert resp.status_code == 200
        results = resp.json()["results"]
        assert [(r["path"], r["score"]) for r in results] == [("c.md", 0.95), ("b.md", 0.6)]
        assert results[0]["description"] == "about c"
        assert results[0]["tags"] == ["type/reference"]
        assert results[0]["chunk"] == "passage c"
        # meiva:note
        # one decision request per candidate, on the configured model, about the query
        assert len(rig.judge.calls) == 3
        assert {model for model, _, _ in rig.judge.calls} == {configured_model}
        assert all("what about c" in state for _, state, _ in rig.judge.calls)

    def test_purpose_selects_the_relevance_question(self, tmp_path: Path, rig: SearchRig) -> None:
        self._pool(tmp_path)
        rig.service.hits = [hit(tmp_path / "a.md")]
        questions: dict[str, Any] = {}
        for purpose in ("recall", "search", "dedup"):
            rig.judge.calls.clear()
            with rig.client(tmp_path) as client:
                resp = client.post(
                    "/api/memory/search", json={"query": "q", "k": 1, "purpose": purpose}
                )
            assert resp.status_code == 200
            questions[purpose] = rig.judge.calls[0][2]
        assert len(set(questions.values())) == 3

    def test_backend_is_asked_for_the_configured_candidate_count(
        self, tmp_path: Path, rig: SearchRig
    ) -> None:
        self._pool(tmp_path)
        rig.configure(memory_rerank_candidates=40)

        with rig.client(tmp_path) as client:
            assert client.post(
                "/api/memory/search", json={"query": "q", "k": 5, "purpose": "search"}
            ).json() == {"results": []}
            client.post("/api/memory/search", json={"query": "q", "k": 60, "purpose": "search"})
        # meiva:note
        # a `k` above the candidate count does not widen the retrieval
        assert rig.service.asked == [40, 40]

    def test_dedup_asks_for_its_own_candidate_count(self, tmp_path: Path, rig: SearchRig) -> None:
        self._pool(tmp_path)
        rig.configure(memory_rerank_candidates=40, memory_dedup_candidates=7)

        with rig.client(tmp_path) as client:
            for purpose, k in (("dedup", 5), ("dedup", 9), ("recall", 5), ("search", 5)):
                client.post("/api/memory/search", json={"query": "q", "k": k, "purpose": purpose})
        # meiva:note
        # dedup ranks its own count whatever `k` is; recall and search keep the shared one
        assert rig.service.asked == [7, 7, 40, 40]

    def test_dedup_candidate_count_defaults_to_five(self) -> None:
        assert settings.services.memory_dedup_candidates == 5

    def test_stale_vector_hits_are_dropped_before_ranking(
        self, tmp_path: Path, rig: SearchRig
    ) -> None:
        self._pool(tmp_path)
        rig.service.hits = [
            hit(tmp_path / "gone.md"),
            hit(tmp_path / "a.md", idx=9),
            hit(tmp_path / "b.md"),
        ]
        with rig.client(tmp_path) as client:
            resp = client.post(
                "/api/memory/search", json={"query": "q", "k": 5, "purpose": "search"}
            )
        assert [r["path"] for r in resp.json()["results"]] == ["b.md"]
        assert len(rig.judge.calls) == 1

    def test_decision_failure_is_a_clear_503_not_vector_order(
        self, tmp_path: Path, rig: SearchRig
    ) -> None:
        from base.api_contracts.agent_contract import IndexerUnavailable

        self._pool(tmp_path)
        rig.service.hits = [hit(tmp_path / "a.md")]
        rig.judge.failure = IndexerUnavailable("memory rerank failed (jev): upstream 503")

        with rig.client(tmp_path) as client:
            resp = client.post(
                "/api/memory/search", json={"query": "q", "k": 5, "purpose": "search"}
            )

        assert resp.status_code == 503
        body = resp.json()
        assert body["reason"] == "indexer_unavailable"
        assert "memory rerank failed" in body["detail"]
        assert "results" not in body


# meiva:note
# Schema validation — query must not be empty, k range.
class TestRequestValidation:
    def test_empty_query_rejected(self) -> None:
        with gateway_test_client() as client:
            resp = client.post(
                "/api/memory/search", json={"query": "", "k": 5, "purpose": "search"}
            )
        assert resp.status_code == 422

    @pytest.mark.parametrize(
        "body", [{"query": "x", "k": 5}, {"query": "x", "k": 5, "purpose": "audit"}]
    )
    def test_missing_or_unknown_purpose_rejected(self, body: dict[str, Any]) -> None:
        with gateway_test_client() as client:
            resp = client.post("/api/memory/search", json=body)
        assert resp.status_code == 422

    def test_blank_query_rejected(self) -> None:
        with gateway_test_client() as client:
            resp = client.post(
                "/api/memory/search", json={"query": "  \n ", "k": 5, "purpose": "search"}
            )
        assert resp.status_code == 422

    def test_k_out_of_range_rejected(self) -> None:
        with gateway_test_client() as client:
            resp = client.post(
                "/api/memory/search", json={"query": "x", "k": 0, "purpose": "search"}
            )
        assert resp.status_code == 422
        with gateway_test_client() as client:
            resp = client.post(
                "/api/memory/search", json={"query": "x", "k": 101, "purpose": "search"}
            )
        assert resp.status_code == 422


class _FailingPriceLookup:
    def __init__(
        self,
        catalog: object,
        seen: list[object],
        failure: Exception,
    ) -> None:
        self._catalog = catalog
        self._seen = seen
        self._failure = failure

    def vendor(self, model: str) -> str | None:
        del model
        return None

    def provenance(self, model: str) -> tuple[str, str] | None:
        del model
        return None

    def rates_at(
        self,
        model: str,
        at: datetime | None,
        input_tokens: int | None,
    ) -> Rates | None:
        del model, at, input_tokens
        self._seen.append(self._catalog)
        raise self._failure


@pytest.mark.parametrize("error_type", [RuntimeError, TypeError])
async def test_query_usage_failure_propagates_without_network_replay(
    tmp_path: Path, rig: SearchRig, error_type: type[Exception]
) -> None:
    rig.embedder.real = True
    rig.configure(gemini_api_key=SecretStr("test-key"))
    app = rig.asgi_app(tmp_path, permits=1)
    failure = error_type("accounting invariant failed")
    seen: list[object] = []
    lookup = _FailingPriceLookup(rig.catalog, seen, failure)
    prices_any: Any = lookup
    embed_catalog = replace(rig.catalog, prices=prices_any)
    lookup._catalog = embed_catalog
    payload = json.dumps(
        {
            "embeddings": [{"values": [1.0] * DIM}],
            "usageMetadata": {"promptTokenCount": 123},
        }
    ).encode()
    calls: list[int] = []

    async def embed_endpoint(scope: Any, receive: Any, send: Any) -> None:
        del scope
        while (await receive()).get("more_body", False):
            pass
        calls.append(1)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": payload})

    async def stays_connected() -> dict[str, str]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    with serve_asgi(embed_endpoint) as url:
        app.state.memory_embedder = GeminiEmbeddingProvider(
            embed_catalog,
            timeout_s=app.state.gateway_config.memory_search.embedding_timeout,
            api_key="test-key",
            endpoint=url,
        )
        request = Request({"type": "http", "app": app}, stays_connected)
        with pytest.raises(error_type) as caught:
            await memory.post_memory_search(
                request, MemorySearchRequest(query="hello", k=1, purpose="search")
            )
        assert len(calls) == 1
    assert caught.value is failure
    assert seen == [embed_catalog]
    assert app.state.memory_search_gate._value == 1
