# meiva:link services/docs/gateway_side/memory_indexer/embeddinggemma.md

from __future__ import annotations

from collections.abc import Callable

from services.derived.embeddinggemma.launch import EmbeddingGemmaSettings
from services.supervision.roster.field_values import optional_text, text, whole


def embeddinggemma_settings(read_field: Callable[[str], object]) -> EmbeddingGemmaSettings:
    def field(name: str) -> object:
        return read_field(f"embeddinggemma_{name}")

    return EmbeddingGemmaSettings(
        host=text(field("host"), "embeddinggemma_host"),
        port=whole(field("port"), "embeddinggemma_port"),
        device=text(field("device"), "embeddinggemma_device"),
        revision=optional_text(field("revision"), "embeddinggemma_revision"),
        model=optional_text(field("model"), "embeddinggemma_model"),
        model_path=str(field("model_path")),
        cache_dir=str(field("cache_dir")),
        python=text(field("python"), "embeddinggemma_python").strip(),
        litert_path=text(field("litert_path"), "embeddinggemma_litert_path").strip(),
        bind_host=text(field("bind_host"), "embeddinggemma_bind_host").strip(),
        max_input_length=whole(field("max_input_length"), "embeddinggemma_max_input_length"),
        api_key_configured=read_field("embeddinggemma_api_key") is not None,
    )
