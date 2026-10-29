# meiva:note
# One switch selects the embedding provider.
#
# `get_provider()` is the single entry point: the indexer daemon (document
# batch path) and the gateway search endpoint (async query path) both take
# their provider from here, keyed by the name supplied by the composition root
# (`MEIVA_EMBEDDING_BACKEND`, default `gemini`). Unknown names fail fast — an
# unrecognized value raises ValueError naming the known providers instead of
# silently falling back to Gemini (a typo would otherwise keep the old
# provider while the operator believes the switch happened).
#
# Each call returns a fresh provider holding the root's captured inputs; construction does no
# network I/O. Gemini alone uses the model catalog for accounting. Every registry entry pairs its constructor with a document-batch
# retry budget, exposed by `worst_case_batch_seconds()` for daemon liveness.
# `get_descriptor()` exposes dim/fingerprint for storage and health probes without
# constructing a billing-capable provider or loading a catalog.

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import httpx

from base.lm.catalog import ModelCatalog
from services.derived.memory_indexer.embeddings import embeddinggemma, gemini
from services.derived.memory_indexer.embeddings.base import EmbeddingProvider


@dataclass(frozen=True)
class EmbeddingDescriptor:
    """The provider's vector space, available without a runtime catalog."""

    name: str
    dim: int
    fingerprint: str


_ProviderEntry = tuple[
    Callable[
        [
            ModelCatalog,
            float,
            str | None,
            httpx.AsyncBaseTransport | None,
            embeddinggemma.EmbeddingGemmaConfig | None,
        ],
        EmbeddingProvider,
    ],
    Callable[[float], float],
    Callable[[str | None], EmbeddingDescriptor],
]


def _require_gemma(
    config: embeddinggemma.EmbeddingGemmaConfig | None,
) -> embeddinggemma.EmbeddingGemmaConfig:
    if config is None:
        raise ValueError("embeddinggemma requires its endpoint, model and revision configuration")
    return config


PROVIDERS: dict[str, _ProviderEntry] = {
    gemini.GeminiEmbeddingProvider.name: (
        lambda catalog, timeout_s, api_key, transport, _config: gemini.GeminiEmbeddingProvider(
            catalog, timeout_s=timeout_s, api_key=api_key, transport=transport
        ),
        gemini.worst_case_batch_seconds,
        lambda _revision: EmbeddingDescriptor(
            gemini.GeminiEmbeddingProvider.name,
            gemini.GeminiEmbeddingProvider.dim,
            gemini.GeminiEmbeddingProvider.fingerprint,
        ),
    ),
    embeddinggemma.NAME: (
        lambda _catalog, timeout_s, _api_key, transport, config: (
            embeddinggemma.EmbeddingGemmaProvider(
                _require_gemma(config), timeout_s=timeout_s, transport=transport
            )
        ),
        lambda timeout_s: timeout_s,
        lambda revision: EmbeddingDescriptor(
            embeddinggemma.NAME,
            embeddinggemma.DIM,
            embeddinggemma.fingerprint_for_revision(revision),
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
    embeddinggemma_config: embeddinggemma.EmbeddingGemmaConfig | None = None,
) -> EmbeddingProvider:
    ctor, _, _ = _provider_entry(name)
    return ctor(catalog, timeout_s, api_key, transport, embeddinggemma_config)


def worst_case_batch_seconds(name: str, *, timeout_seconds: float) -> float:
    """Return the configured provider's full document-batch retry budget."""
    _, batch_seconds, _ = _provider_entry(name)
    return batch_seconds(timeout_seconds)


def get_descriptor(name: str, *, revision: str | None = None) -> EmbeddingDescriptor:
    """Read the configured provider's metadata without constructing a provider."""
    _, _, descriptor = _provider_entry(name)
    return descriptor(revision)


def embeddinggemma_config(
    name: str,
    *,
    endpoint: str | None,
    model: str | None,
    revision: str | None,
    api_key: str | None,
) -> embeddinggemma.EmbeddingGemmaConfig | None:
    # meiva:note
    # Validate the selected backend's raw configuration without touching the network.
    if name != embeddinggemma.NAME:
        return None
    if endpoint is None or model is None or revision is None:
        raise ValueError(
            "embeddinggemma requires MEIVA_EMBEDDINGGEMMA_ENDPOINT, "
            "MEIVA_EMBEDDINGGEMMA_MODEL and MEIVA_EMBEDDINGGEMMA_REVISION"
        )
    return embeddinggemma.EmbeddingGemmaConfig(endpoint, model, revision, api_key)
