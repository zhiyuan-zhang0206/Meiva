# meiva:link services/docs/gateway_side/memory_indexer/embeddinggemma.md

from __future__ import annotations

from base.cluster.listener_plan import LOOPBACK_ALIASES
from base.host.net.url_secret import url_with_host
from services.derived.embeddinggemma.contract import (
    DEFAULT_MODEL,
    EMBEDDINGS_PATH,
    LOOPBACK,
    require_artifact_sha256,
)

_SELF_UNSET = "localhost"


def _normalize(host: str) -> str:
    bare = host.strip().lower().removeprefix("[").removesuffix("]")
    return _SELF_UNSET if bare in LOOPBACK_ALIASES else bare


def hosted_here(host: str, *, self_host: str) -> bool:
    placement = _normalize(host)
    return bool(placement) and placement == (_normalize(self_host) or _SELF_UNSET)


def hosting_problem(host: str, *, self_host: str, revision: str | None) -> str | None:
    if not host.strip():
        return "no managed EmbeddingGemma service (MEIVA_EMBEDDINGGEMMA_HOST is empty)"
    if not hosted_here(host, self_host=self_host):
        return f"EmbeddingGemma is hosted on {host.strip()}, not on this machine"
    try:
        require_artifact_sha256(revision)
    except ValueError as exc:
        return str(exc)
    return None


def managed_endpoint(host: str, port: int, *, self_host: str) -> str:
    target = LOOPBACK if hosted_here(host, self_host=self_host) else host.strip()
    return url_with_host(f"http://localhost:{port}{EMBEDDINGS_PATH}", target)


def client_target(
    *,
    endpoint: str | None,
    model: str | None,
    host: str,
    port: int,
    self_host: str,
) -> tuple[str | None, str | None]:
    if not host.strip():
        return endpoint, model
    return (
        managed_endpoint(host, port, self_host=self_host) if endpoint is None else endpoint,
        DEFAULT_MODEL if model is None else model,
    )
