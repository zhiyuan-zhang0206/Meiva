# meiva:note
# EmbeddingGemma 2 text retrieval over the raw-input LiteRT-LM embedding API.

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
import numpy as np
from pydantic import BaseModel, ConfigDict, ValidationError

from services.derived.memory_indexer.embeddings.base import EmbeddingAPIError
from services.derived.memory_indexer.embeddings.sync_call import run_sync

NAME = "embeddinggemma"
DIM = 768
QUERY_PREFIX = "task: search result | query: "
DOCUMENT_PREFIX = "title: none | text: "
INSERT_SPECIAL_TOKENS = True


def fingerprint_for_revision(revision: str | None) -> str:
    # meiva:note
    # Identify an operator-pinned artifact and this exact retrieval protocol.
    if revision is None or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision) is None:
        raise ValueError(
            "MEIVA_EMBEDDINGGEMMA_REVISION must pin a lowercase 40-hex checkpoint commit "
            "or 64-hex artifact SHA256"
        )
    protocol = (
        revision,
        DIM,
        QUERY_PREFIX,
        DOCUMENT_PREFIX,
        INSERT_SPECIAL_TOKENS,
        "l2-normalized-float32",
    )
    return hashlib.sha256(json.dumps(protocol).encode()).hexdigest()


@dataclass(frozen=True)
class EmbeddingGemmaConfig:
    # meiva:note
    # The raw-input server locator and independently pinned semantic artifact.

    endpoint: str
    model: str
    revision: str
    api_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        url = urlsplit(self.endpoint)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or not url.path.endswith("/v1/embeddings")
        ):
            raise ValueError("MEIVA_EMBEDDINGGEMMA_ENDPOINT must be an HTTP(S) /v1/embeddings URL")
        if not self.model.strip() or self.model != self.model.strip():
            raise ValueError("MEIVA_EMBEDDINGGEMMA_MODEL must name the server's pinned model")
        fingerprint_for_revision(self.revision)


class _Embedding(BaseModel):
    model_config = ConfigDict(strict=True)
    index: int
    embedding: list[float]


class _Response(BaseModel):
    model_config = ConfigDict(strict=True)
    model: str
    data: list[_Embedding]


def _vectors(body: object, *, model: str, count: int) -> np.ndarray:
    try:
        response = _Response.model_validate(body)
    except ValidationError as exc:
        raise EmbeddingAPIError("malformed EmbeddingGemma response") from exc
    if response.model != model:
        raise EmbeddingAPIError("EmbeddingGemma response model does not match the configured model")
    indices = [row.index for row in response.data]
    if sorted(indices) != list(range(count)):
        raise EmbeddingAPIError("EmbeddingGemma response must contain each requested index once")
    try:
        vectors = np.asarray(
            [row.embedding for row in sorted(response.data, key=lambda row: row.index)],
            dtype=np.float32,
        )
    except (ValueError, OverflowError) as exc:
        raise EmbeddingAPIError("malformed EmbeddingGemma vectors") from exc
    if vectors.shape != (count, DIM) or not np.isfinite(vectors).all():
        raise EmbeddingAPIError(
            f"EmbeddingGemma vectors must be finite with shape ({count}, {DIM})"
        )
    norms = np.linalg.norm(vectors.astype(np.float64), axis=1, keepdims=True)
    if np.any(norms == 0):
        raise EmbeddingAPIError("EmbeddingGemma returned a zero vector")
    return (vectors / norms).astype(np.float32)


class EmbeddingGemmaProvider:
    # meiva:note
    # One bounded HTTP attempt per call; patrol/caller retries own recovery.

    name = NAME
    dim = DIM

    def __init__(
        self,
        config: EmbeddingGemmaConfig,
        *,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("embedding timeout must be finite and positive")
        self.fingerprint = fingerprint_for_revision(config.revision)
        self._config = config
        self._timeout_s = timeout_s
        self._transport = transport

    async def _embed(self, texts: list[str], prefix: str) -> np.ndarray:
        if not texts:
            return np.empty((0, DIM), dtype=np.float32)
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("embedding inputs must be non-empty strings")
        headers: dict[str, str] = {}
        if self._config.api_key is not None:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        payload = {
            "model": self._config.model,
            "input": [prefix + text for text in texts],
            "encoding_format": "float",
            "dimensions": DIM,
            "normalize": True,
            "insert_special_tokens": INSERT_SPECIAL_TOKENS,
        }
        try:
            async with asyncio.timeout(self._timeout_s):
                async with httpx.AsyncClient(
                    timeout=self._timeout_s, transport=self._transport
                ) as client:
                    async with client.stream(
                        "POST", self._config.endpoint, json=payload, headers=headers
                    ) as response:
                        response.raise_for_status()
                        body = json.loads(await response.aread())
        except (httpx.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            raise EmbeddingAPIError(f"EmbeddingGemma embed failed ({type(exc).__name__})") from exc
        return _vectors(body, model=self._config.model, count=len(texts))

    def embed_batch(self, texts: list[str]) -> np.ndarray:
        return run_sync(lambda: self._embed(texts, DOCUMENT_PREFIX))

    def embed_query(self, text: str) -> np.ndarray:
        return run_sync(lambda: self._embed([text], QUERY_PREFIX))[0]

    async def embed_query_async(self, text: str) -> np.ndarray:
        return (await self._embed([text], QUERY_PREFIX))[0]
