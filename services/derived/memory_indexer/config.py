# meiva:note
# The memory-indexer daemon's tuning and the configuration values its root reads once.

from __future__ import annotations

from dataclasses import dataclass, field

from base.config import ConfigBoot
from services.derived.memory_indexer.backends.base import BATCH_COMMIT_TIMEOUT_S
from services.derived.memory_indexer.embeddings import factory
from services.derived.memory_indexer.embeddings.embeddinggemma import NAME, EmbeddingGemmaConfig
from services.derived.memory_indexer.patrol_plan import liveness_ceiling_s


@dataclass(frozen=True)
class MemoryIndexerConfig:
    loop_interval_s: float = 1.0

    patrol_interval_s: float = 900.0

    retry_backoff_s: float = 60.0

    retry_limit: int = 3

    excluded_basenames: tuple[str, ...] = ("MEMORY.md", "index.md")

    reconcile_chunk_paths: int = 64

    batch_size: int = 32
    """Texts per embedding call: `embed_content` accepts several inputs, so batching amortizes round-trips."""

    def __post_init__(self) -> None:
        if self.loop_interval_s <= 0:
            raise ValueError("loop_interval_s must be positive")
        if self.patrol_interval_s <= 0:
            raise ValueError("patrol_interval_s must be positive")
        if self.retry_backoff_s <= 0:
            raise ValueError("retry_backoff_s must be positive")
        if self.retry_limit < 0:
            raise ValueError("retry_limit must not be negative")
        if self.reconcile_chunk_paths < 1:
            raise ValueError("reconcile_chunk_paths must be at least 1")
        if self.batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        for name in self.excluded_basenames:
            if not name or "/" in name or "\\" in name:
                raise ValueError(f"excluded_basenames entries must be plain file names: {name!r}")


# meiva:note
# What the indexer daemon takes from the configuration owner, read once by its root: the
# embedding provider and its call budget, the storage backend and the address of the search
# service. All of it is restart-bound; nothing below the root reads the owner again.
@dataclass(frozen=True)
class MemoryIndexerInputs:
    embedding_name: str
    embed_timeout_s: float
    api_key: str | None = field(repr=False)
    backend_name: str
    search_uri: str
    embeddinggemma_config: EmbeddingGemmaConfig | None = None

    @classmethod
    def from_boot(cls, boot: ConfigBoot) -> MemoryIndexerInputs:
        services = boot.view.services
        key = boot.view.lm.gemini_api_key
        name = services.embedding_backend
        gemma_config = None
        if name == NAME:
            gemma_key = services.embeddinggemma_api_key
            gemma_config = factory.embeddinggemma_config(
                name,
                endpoint=services.embeddinggemma_endpoint,
                model=services.embeddinggemma_model,
                revision=services.embeddinggemma_revision,
                api_key=None if gemma_key is None else gemma_key.get_secret_value(),
            )
        return cls(
            embedding_name=services.embedding_backend,
            embed_timeout_s=services.memory_embed_timeout_seconds,
            api_key=None if key is None else key.get_secret_value(),
            backend_name=services.memory_search_backend,
            search_uri=services.memory_search_uri,
            embeddinggemma_config=gemma_config,
        )


def liveness_timeout_seconds(name: str, *, timeout_seconds: float) -> float:
    """Cover one selected provider batch plus local scheduling and commit slack."""
    return liveness_ceiling_s(
        max(
            factory.worst_case_batch_seconds(name, timeout_seconds=timeout_seconds),
            BATCH_COMMIT_TIMEOUT_S,
        )
    )
