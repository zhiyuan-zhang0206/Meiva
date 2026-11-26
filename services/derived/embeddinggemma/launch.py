# meiva:link services/docs/gateway_side/memory_indexer/embeddinggemma.md

from __future__ import annotations

from dataclasses import dataclass

from services.derived.embeddinggemma.config import ServerConfig
from services.derived.embeddinggemma.contract import DEFAULT_MODEL, parse_device
from services.derived.embeddinggemma.placement import hosting_problem


@dataclass(frozen=True)
class EmbeddingGemmaSettings:
    host: str
    port: int
    device: str
    revision: str | None
    model: str | None
    model_path: str
    cache_dir: str
    python: str
    litert_path: str
    bind_host: str
    max_input_length: int
    api_key_configured: bool


def server_config(settings: EmbeddingGemmaSettings) -> ServerConfig:
    return ServerConfig(
        model_path=settings.model_path,
        device=parse_device(settings.device),
        revision=settings.revision or "",
        model=DEFAULT_MODEL if settings.model is None else settings.model,
        cache_dir=settings.cache_dir,
        max_input_length=settings.max_input_length,
        bind_host=settings.bind_host,
        port=settings.port,
        litert_path=settings.litert_path or None,
        require_api_key=settings.api_key_configured,
    )


def launch_problem(settings: EmbeddingGemmaSettings, *, self_host: str) -> str | None:
    problem = hosting_problem(settings.host, self_host=self_host, revision=settings.revision)
    if problem is not None:
        return problem
    try:
        server_config(settings)
    except ValueError as exc:
        return f"invalid EmbeddingGemma configuration: {exc}"
    return None
