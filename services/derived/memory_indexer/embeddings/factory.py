"""One switch selects the embedding provider.

`get_provider()` is the single entry point: the indexer daemon (document
batch path) and the gateway search endpoint (async query path) both take
their provider from here, keyed by the name supplied by the composition root
(`MEIVA_EMBEDDING_BACKEND`, default `gemini`). Unknown names fail fast — an
unrecognized value raises ValueError naming the known providers instead of
silently falling back to Gemini (a typo would otherwise keep the old
provider while the operator believes the switch happened).

Each call returns a fresh provider holding the service root's catalog; construction does no
network I/O. Every registry entry pairs its constructor with a document-batch
retry budget, exposed by `worst_case_batch_seconds()` for daemon liveness.
`get_descriptor()` exposes dim/fingerprint for storage and health probes without
constructing a billing-capable provider or loading a catalog.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import httpx

from base.lm.catalog import ModelCatalog
from services.derived.memory_indexer.embeddings import gemini
from services.derived.memory_indexer.embeddings.base import EmbeddingProvider


@dataclass(frozen=True)
class EmbeddingDescriptor:
    """The provider's vector space, available without a runtime catalog."""

    name: str
    dim: int
    fingerprint: str


_ProviderEntry = tuple[
    Callable[[ModelCatalog, float, str | None, httpx.AsyncBaseTransport | None], EmbeddingProvider],
    Callable[[float], float],
    EmbeddingDescriptor,
]
PROVIDERS: dict[str, _ProviderEntry] = {
    gemini.GeminiEmbeddingProvider.name: (
        lambda catalog, timeout_s, api_key, transport=None: gemini.GeminiEmbeddingProvider(
            catalog, timeout_s=timeout_s, api_key=api_key, transport=transport
        ),
        gemini.worst_case_batch_seconds,
        EmbeddingDescriptor(
            gemini.GeminiEmbeddingProvider.name,
            gemini.GeminiEmbeddingProvider.dim,
            gemini.GeminiEmbeddingProvider.fingerprint,
        ),
    ),
}


def _provider_entry(name: str) -> _ProviderEntry:
    try:
        return PROVIDERS[name]
    except KeyError:
        known = ", ".join(sorted(PROVIDERS))
        raise ValueError(f"unknown embedding provider {name!r} (known: {known})") from None


# meiva:note
# Construct the provider `name` selects (the root read it from `MEIVA_EMBEDDING_BACKEND`);
# unknown names fail fast.
def get_provider(
    name: str,
    *,
    catalog: ModelCatalog,
    timeout_s: float,
    api_key: str | None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> EmbeddingProvider:
    ctor, _, _ = _provider_entry(name)
    return ctor(catalog, timeout_s, api_key, transport)


def worst_case_batch_seconds(name: str, *, timeout_seconds: float) -> float:
    """Return the configured provider's full document-batch retry budget."""
    _, batch_seconds, _ = _provider_entry(name)
    return batch_seconds(timeout_seconds)


def get_descriptor(name: str) -> EmbeddingDescriptor:
    """Read the configured provider's metadata without constructing a provider."""
    _, _, descriptor = _provider_entry(name)
    return descriptor
