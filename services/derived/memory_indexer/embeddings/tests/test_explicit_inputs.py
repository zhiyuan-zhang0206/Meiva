from __future__ import annotations

import numpy as np
import pytest

from base.lm.catalog import ModelCatalog
from services.derived.memory_indexer.embeddings.gemini import DIM, GeminiEmbeddingProvider
from services.derived.memory_indexer.embeddings.tests.test_embeddings import (
    AsyncClient,
    patch_client,
)


async def test_two_providers_use_their_own_key_and_timeout(
    monkeypatch: pytest.MonkeyPatch, model_catalog: ModelCatalog
) -> None:
    fake = AsyncClient(vectors=[[1.0] * DIM])
    patch_client(monkeypatch, fake)
    one = GeminiEmbeddingProvider(model_catalog, timeout_s=1.25, api_key="first-key")
    two = GeminiEmbeddingProvider(model_catalog, timeout_s=2.5, api_key="second-key")
    np.testing.assert_equal(one.embed_batch([]).shape, (0, DIM))
    assert fake.call_count == 0
    await one.embed_query_async("first")
    await two.embed_query_async("second")
    await one.embed_query_async("again")
    assert [(call["headers"]["x-goog-api-key"], call["timeout"]) for call in fake.calls] == [
        ("first-key", 1.25),
        ("second-key", 2.5),
        ("first-key", 1.25),
    ]
