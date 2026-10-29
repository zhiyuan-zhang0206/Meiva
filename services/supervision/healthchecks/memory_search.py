"""Read-only health probes for memory search; the root supervisor owns recovery."""

from __future__ import annotations

import json

from base.daemon.health import DaemonProbe
from services.derived.memory_indexer.backends.base import EMBEDDING_SPACE_HEADER, INDEX_API_PREFIX

_TIMEOUT_S = 3.0


# meiva:note
# One POST /search with a zero vector returns the raw decoded JSON, or the down
# verdict from an HTTP/decoding failure. The verdict owner validates the body
# shape; provider configuration errors fail before the request.
def post_search(uri: str, *, embedding_name: str, embedding_revision: str | None = None) -> object:
    import httpx

    from services.derived.memory_indexer.embeddings.factory import get_descriptor

    descriptor = get_descriptor(embedding_name, revision=embedding_revision)
    try:
        resp = httpx.post(
            f"{uri}{INDEX_API_PREFIX}/search",
            json={"vector": [0.0] * descriptor.dim, "k": 1},
            headers={EMBEDDING_SPACE_HEADER: descriptor.fingerprint},
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return DaemonProbe.down(f"POST /search failed ({type(exc).__name__}: {exc})")


# meiva:note
# The verdict on one search answer: only a JSON object can carry the service's
# `hits` member. Membership in a string or array is not protocol evidence.
def search_verdict(answer: object) -> DaemonProbe:
    if isinstance(answer, DaemonProbe):
        return answer
    if not isinstance(answer, dict):
        return DaemonProbe.down("POST /search answered without an object payload")
    if "hits" in answer:
        return DaemonProbe.up("POST /search answered with a hits payload")
    return DaemonProbe.down("POST /search answered without a hits payload")


# meiva:note
# Probe the roster's captured endpoint and provider with a real search. Protocol
# failures report down; an unknown provider remains a configuration error and
# propagates to the caller before any request.
def memory_search_probe(
    *, uri: str, embedding_name: str, embedding_revision: str | None = None
) -> DaemonProbe:
    try:
        return search_verdict(
            post_search(uri, embedding_name=embedding_name, embedding_revision=embedding_revision)
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return DaemonProbe.down(f"protocol probe failed: {exc}")
