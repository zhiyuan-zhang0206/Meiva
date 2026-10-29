from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import httpx
import numpy as np
import pytest

from base.lm.catalog import ModelCatalog
from services.derived.memory_indexer.embeddings import embeddinggemma, factory
from services.derived.memory_indexer.embeddings.base import EmbeddingAPIError

REVISION = "f9972ac22e696a105c482202646979e33e4b5a1e"


def _config() -> embeddinggemma.EmbeddingGemmaConfig:
    return embeddinggemma.EmbeddingGemmaConfig(
        "http://127.0.0.1:9379/v1/embeddings", "/models/pinned.litertlm,gpu", REVISION
    )


def _response(model: str, count: int = 1) -> dict[str, Any]:
    return {
        "model": model,
        "data": [
            {
                "index": index,
                "embedding": [
                    1.0 if column == index else 0.0 for column in range(embeddinggemma.DIM)
                ],
            }
            for index in reversed(range(count))
        ],
    }


def test_document_and_query_prefixes_are_applied_once_and_results_are_ordered() -> None:
    seen: list[dict[str, object]] = []
    config = replace(_config(), api_key="local-test-secret")

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        assert request.headers["Authorization"] == "Bearer local-test-secret"
        return httpx.Response(200, json=_response(config.model, len(body["input"])))

    provider = embeddinggemma.EmbeddingGemmaProvider(
        config, timeout_s=1.0, transport=httpx.MockTransport(respond)
    )
    documents = provider.embed_batch(["first", "second"])
    query = provider.embed_query("find")
    async_query = asyncio.run(provider.embed_query_async("again"))
    assert documents.shape == (2, 768)
    assert query.shape == async_query.shape == (768,)
    assert documents.dtype == np.float32
    assert documents.argmax(axis=1).tolist() == [0, 1]
    np.testing.assert_allclose(np.linalg.norm(documents, axis=1), 1.0, rtol=1e-6)
    assert [row["input"] for row in seen] == [
        ["title: none | text: first", "title: none | text: second"],
        ["task: search result | query: find"],
        ["task: search result | query: again"],
    ]
    assert all(row["normalize"] is True and row["insert_special_tokens"] is True for row in seen)
    assert all(row["encoding_format"] == "float" and row["dimensions"] == 768 for row in seen)
    assert "local-test-secret" not in repr(config)


@pytest.mark.parametrize(
    "invalid", ["missing", "duplicate", "width", "zero", "nan", "model", "index_type"]
)
def test_malformed_responses_never_become_search_vectors(invalid: str) -> None:
    config = _config()

    def respond(request: httpx.Request) -> httpx.Response:
        body = _response(config.model)
        if invalid == "missing":
            body["data"] = []
        elif invalid == "duplicate":
            body["data"] = body["data"] * 2
        elif invalid == "model":
            body["model"] = "another-model"
        elif invalid == "index_type":
            body["data"][0]["index"] = True
        else:
            body["data"][0]["embedding"] = {
                "width": [1.0],
                "zero": [0.0] * 768,
                "nan": [float("nan")] * 768,
            }[invalid]
        return httpx.Response(200, content=json.dumps(body).encode())

    provider = embeddinggemma.EmbeddingGemmaProvider(
        config, timeout_s=1.0, transport=httpx.MockTransport(respond)
    )
    with pytest.raises(EmbeddingAPIError):
        provider.embed_query("find")


def test_artifact_revision_controls_space_independently_of_service_locator() -> None:
    config = _config()
    changed_locator = replace(
        config, endpoint="http://another-host/v1/embeddings", model="another-path"
    )
    first = embeddinggemma.EmbeddingGemmaProvider(config, timeout_s=1)
    moved = embeddinggemma.EmbeddingGemmaProvider(changed_locator, timeout_s=1)
    changed = embeddinggemma.EmbeddingGemmaProvider(replace(config, revision="a" * 64), timeout_s=1)
    assert first.fingerprint == moved.fingerprint
    assert first.fingerprint != changed.fingerprint
    assert len(first.fingerprint) <= 64
    descriptor = factory.get_descriptor("embeddinggemma", revision=REVISION)
    assert descriptor.fingerprint == first.fingerprint
    assert descriptor.dim == first.dim == 768


def test_factory_preserves_metadata_and_uses_local_auth_only(model_catalog: ModelCatalog) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_response(_config().model))

    provider = factory.get_provider(
        "embeddinggemma",
        catalog=model_catalog,
        timeout_s=1,
        api_key="gemini-key-must-not-leak",
        embeddinggemma_config=_config(),
        transport=httpx.MockTransport(respond),
    )
    provider.embed_query("find")
    assert "Authorization" not in calls[0].headers
    assert "x-goog-api-key" not in calls[0].headers
    assert factory.worst_case_batch_seconds("embeddinggemma", timeout_seconds=3) == 3


def test_missing_or_mutable_revision_is_rejected_before_network() -> None:
    for revision in (None, "main", "latest", ""):
        with pytest.raises(ValueError, match="REVISION"):
            factory.get_descriptor("embeddinggemma", revision=revision)
    with pytest.raises(ValueError, match="ENDPOINT"):
        factory.embeddinggemma_config(
            "embeddinggemma", endpoint=None, model=None, revision=None, api_key=None
        )


async def test_async_deadline_covers_a_stalled_response_and_does_not_retry() -> None:
    calls = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(10)
        return httpx.Response(200, json=_response(_config().model))

    provider = embeddinggemma.EmbeddingGemmaProvider(
        _config(), timeout_s=0.02, transport=httpx.MockTransport(respond)
    )
    with pytest.raises(EmbeddingAPIError) as caught:
        await provider.embed_query_async("find")
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert calls == 1


def test_empty_batch_has_no_network_call() -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError("empty batch must not call the server")

    provider = embeddinggemma.EmbeddingGemmaProvider(
        _config(), timeout_s=1, transport=httpx.MockTransport(forbidden)
    )
    assert provider.embed_batch([]).shape == (0, 768)
