from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import Any

import httpx
import numpy as np
import pytest

from base.config import ConfigBoot
from base.host.net.resilience import ExponentialBackoff, Policy
from base.lm.catalog import ModelCatalog
from services.derived.memory_indexer.embeddings import factory, gemini
from services.derived.memory_indexer.embeddings.base import EmbeddingAPIError
from services.derived.memory_indexer.embeddings.gemini import DIM, GeminiEmbeddingProvider
from services.derived.memory_indexer.embeddings.tests.test_embeddings import (
    API_KEY,
    TIMEOUT_S,
    build_provider,
    embedding_server,
)
from services.derived.memory_indexer.embeddings.tests.test_embeddings import (
    trickle_server as trickle_server,
)


def test_worst_case_single_attempt_has_no_sleep() -> None:
    from dataclasses import replace
    from unittest.mock import Mock

    backoff = Mock(side_effect=AssertionError("one attempt must not evaluate backoff"))
    policy = replace(gemini.EMBED_POLICY, max_attempts=1, backoff=backoff)
    assert gemini.worst_case_batch_seconds(TIMEOUT_S, policy) == TIMEOUT_S
    backoff.assert_not_called()


def test_factory_default_is_gemini(model_catalog: ModelCatalog) -> None:
    name = ConfigBoot().view.services.embedding_backend
    assert name == "gemini"
    assert isinstance(
        factory.get_provider(name, catalog=model_catalog, timeout_s=TIMEOUT_S, api_key=API_KEY),
        GeminiEmbeddingProvider,
    )


def test_factory_unknown_backend_fails_fast(model_catalog: ModelCatalog) -> None:
    with pytest.raises(ValueError, match="unknown embedding provider"):
        factory.get_provider("openai", catalog=model_catalog, timeout_s=TIMEOUT_S, api_key=API_KEY)


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("attempts", [1, 3])
def test_embed_trickle_total_deadline(
    trickle_server: tuple[str, list[float]],
    mode: str,
    attempts: int,
    model_catalog: ModelCatalog,
) -> None:
    endpoint, requests = trickle_server
    timeout = 0.25
    policy = Policy(
        max_attempts=attempts,
        backoff=ExponentialBackoff(base=0.001, factor=1, cap=0.001),
        jitter_span=0,
    )
    started = time.monotonic()
    with pytest.raises(EmbeddingAPIError, match="exceeded the deadline") as error:
        if mode == "sync":
            gemini.embed(
                ["hello"],
                "RETRIEVAL_DOCUMENT",
                catalog=model_catalog,
                endpoint=endpoint,
                policy=policy,
                timeout_s=timeout,
                api_key=API_KEY,
            )
        else:
            asyncio.run(
                gemini.embed_async(
                    ["hello"],
                    "RETRIEVAL_QUERY",
                    catalog=model_catalog,
                    endpoint=endpoint,
                    policy=policy,
                    timeout_s=timeout,
                    api_key=API_KEY,
                )
            )
    elapsed = time.monotonic() - started
    assert isinstance(error.value.__cause__, httpx.ReadTimeout)
    assert len(requests) == attempts
    assert attempts * timeout <= elapsed < attempts * (timeout + 0.2)
    assert elapsed < 2


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("attempts", [1, 3])
def test_embed_compressed_trickle_deadline(
    attempts: int,
    mode: str,
    model_catalog: ModelCatalog,
) -> None:
    timeout = 0.2
    policy = Policy(
        max_attempts=attempts,
        backoff=ExponentialBackoff(base=0.001, factor=1, cap=0.001),
        jitter_span=0,
    )
    with embedding_server(compressed=True) as (endpoint, requests):
        started = time.monotonic()
        with pytest.raises(EmbeddingAPIError, match="exceeded the deadline") as error:
            if mode == "sync":
                gemini.embed(
                    ["hello"],
                    "RETRIEVAL_DOCUMENT",
                    catalog=model_catalog,
                    endpoint=endpoint,
                    policy=policy,
                    timeout_s=timeout,
                    api_key=API_KEY,
                )
            else:
                asyncio.run(
                    gemini.embed_async(
                        ["hello"],
                        "RETRIEVAL_QUERY",
                        catalog=model_catalog,
                        endpoint=endpoint,
                        policy=policy,
                        timeout_s=timeout,
                        api_key=API_KEY,
                    )
                )
        elapsed = time.monotonic() - started
        assert isinstance(error.value.__cause__, httpx.ReadTimeout)
        assert len(requests) == attempts
        assert attempts * timeout <= elapsed < attempts * (timeout + 0.2)
        assert elapsed < 2


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("framing", ["extension", "trailer"])
@pytest.mark.parametrize("attempts", [1, 3])
def test_embed_framing_drip_deadline(
    mode: str,
    framing: str,
    attempts: int,
    model_catalog: ModelCatalog,
) -> None:
    from base.host.net import resilience

    timeout = 0.2
    classified: list[Exception] = []

    def classify(exc: Exception) -> bool:
        classified.append(exc)
        return resilience.http_classifier(exc)

    policy = Policy(
        max_attempts=attempts,
        backoff=ExponentialBackoff(base=0.001, factor=1, cap=0.001),
        jitter_span=0,
        classify=classify,
    )
    framing_started = threading.Event()
    with embedding_server(framing=framing, framing_started=framing_started) as (
        endpoint,
        requests,
    ):
        started = time.monotonic()
        with pytest.raises(EmbeddingAPIError, match="exceeded the deadline") as error:
            if mode == "sync":
                gemini.embed(
                    ["hello"],
                    "RETRIEVAL_DOCUMENT",
                    catalog=model_catalog,
                    endpoint=endpoint,
                    policy=policy,
                    timeout_s=timeout,
                    api_key=API_KEY,
                )
            else:
                asyncio.run(
                    gemini.embed_async(
                        ["hello"],
                        "RETRIEVAL_QUERY",
                        catalog=model_catalog,
                        endpoint=endpoint,
                        policy=policy,
                        timeout_s=timeout,
                        api_key=API_KEY,
                    )
                )
        elapsed = time.monotonic() - started
        assert isinstance(error.value.__cause__, httpx.ReadTimeout)
        assert len(classified) == attempts - 1
        assert all(isinstance(exc, httpx.ReadTimeout) for exc in classified)
        # meiva:note
        # The total deadline can expire before a request reaches the server.
        # Retry failures, rather than observed POSTs, count client attempts.
        assert 1 <= len(requests) <= attempts
        assert framing_started.is_set()
        assert attempts * timeout <= elapsed < attempts * (timeout + 0.2)
        assert elapsed < 2


@pytest.mark.parametrize("chunked", [False, True])
def test_embed_gzip_response_round_trip(chunked: bool) -> None:
    with embedding_server(compressed=True, chunked=chunked, drip=False) as (endpoint, requests):
        result = build_provider(endpoint=endpoint).embed_batch(["hello"])
        np.testing.assert_array_equal(result, np.ones((1, DIM), dtype=np.float32))
        assert len(requests) == 1


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("status", [400, 429])
def test_embed_slow_error_body_preserves_status(
    retry_waits: list[float], mode: str, status: int, model_catalog: ModelCatalog
) -> None:
    from base.host.net import resilience

    timeout = 0.3
    classified: list[Exception] = []

    def classify(exc: Exception) -> bool:
        classified.append(exc)
        return resilience.http_classifier(exc)

    policy = Policy(
        max_attempts=2,
        backoff=ExponentialBackoff(base=0.001, factor=1, cap=0.001),
        jitter_span=0,
        classify=classify,
    )
    with embedding_server(error_status=status, drip=False) as (endpoint, requests):

        def invoke() -> np.ndarray:
            if mode == "sync":
                return gemini.embed(
                    ["hello"],
                    "RETRIEVAL_DOCUMENT",
                    catalog=model_catalog,
                    endpoint=endpoint,
                    policy=policy,
                    timeout_s=timeout,
                    api_key=API_KEY,
                )
            return asyncio.run(
                gemini.embed_async(
                    ["hello"],
                    "RETRIEVAL_QUERY",
                    catalog=model_catalog,
                    endpoint=endpoint,
                    policy=policy,
                    timeout_s=timeout,
                    api_key=API_KEY,
                )
            )

        started = time.monotonic()
        if status == 400:
            with pytest.raises(EmbeddingAPIError, match="Gemini embed HTTP 400") as error:
                invoke()
            assert isinstance(error.value.__cause__, httpx.HTTPStatusError)
            assert len(requests) == 1
            assert retry_waits == []
        else:
            result = invoke()
            np.testing.assert_array_equal(result, np.ones((1, DIM), dtype=np.float32))
            assert len(requests) == 2
            assert retry_waits == [7.0]
        elapsed = time.monotonic() - started
        assert elapsed < 0.25
        assert len(classified) == 1
        status_error = classified[0]
        assert isinstance(status_error, httpx.HTTPStatusError)
        assert status_error.response.status_code == status
        assert resilience.extract_retry_after(status_error) == 7.0
        assert not status_error.response.is_stream_consumed


def test_sync_embed_slow_resolver_deadline(
    monkeypatch: pytest.MonkeyPatch, model_catalog: ModelCatalog
) -> None:
    real_getaddrinfo = socket.getaddrinfo
    finished = threading.Event()
    resolver_calls: list[object] = []

    def slow_getaddrinfo(host: object, *args: Any, **kwargs: Any) -> Any:
        resolver_calls.append(host)
        try:
            time.sleep(0.5)
            return real_getaddrinfo("127.0.0.1", *args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(socket, "getaddrinfo", slow_getaddrinfo)
    with embedding_server(drip=False) as (endpoint, requests):
        started = time.monotonic()
        try:
            with pytest.raises(EmbeddingAPIError, match="exceeded the deadline") as error:
                gemini.embed(
                    ["hello"],
                    "RETRIEVAL_DOCUMENT",
                    catalog=model_catalog,
                    endpoint=endpoint.replace("127.0.0.1", "resolver.test"),
                    policy=Policy(max_attempts=1),
                    timeout_s=0.1,
                    api_key=API_KEY,
                )
            elapsed = time.monotonic() - started
            assert isinstance(error.value.__cause__, httpx.ReadTimeout)
            assert len(resolver_calls) == 1
            assert not requests
            assert 0.1 <= elapsed < 0.3
            assert not finished.is_set()
        finally:
            assert finished.wait(timeout=2)


def test_metadata_descriptor_is_the_provider_declaration() -> None:
    descriptor = factory.get_descriptor("gemini")
    assert descriptor.name == GeminiEmbeddingProvider.name
    assert descriptor.dim == GeminiEmbeddingProvider.dim
    assert descriptor.fingerprint == GeminiEmbeddingProvider.fingerprint


def test_unknown_metadata_provider_fails_fast() -> None:
    with pytest.raises(ValueError, match="unknown embedding provider"):
        factory.get_descriptor("unknown-provider")
