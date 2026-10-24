"""Read-only health probes for memory search; the root supervisor owns recovery."""

from __future__ import annotations

import json

from base.daemon.health import DaemonProbe

_TIMEOUT_S = 3.0


def post_search(uri: str, *, embedding_name: str) -> dict[str, object] | DaemonProbe:
    """One POST /search with a zero vector — the body dict when it
    answered, or the not-alive verdict that trying produced (fail
    closed: any failure means "not alive", per the module docstring)."""
    import httpx

    from services.derived.memory_indexer.embeddings.factory import get_descriptor

    dim = get_descriptor(embedding_name).dim
    try:
        resp = httpx.post(
            f"{uri}/search",
            json={"vector": [0.0] * dim, "k": 1},
            timeout=_TIMEOUT_S,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        return DaemonProbe.down(f"POST /search failed ({type(exc).__name__}: {exc})")


# meiva:note
# The verdict on one search answer: alive only for a body that carries a `hits` payload, since a
# foreign process answering 200 on the port is not the search service.
def search_verdict(answer: dict[str, object] | DaemonProbe) -> DaemonProbe:
    if isinstance(answer, DaemonProbe):
        return answer
    if "hits" in answer:
        return DaemonProbe.up("POST /search answered with a hits payload")
    return DaemonProbe.down("POST /search answered without a hits payload")


# meiva:note
# Probe the roster's captured endpoint and provider with a real search. Protocol
# failures report down; an unknown provider remains a configuration error and
# propagates to the caller before any request.
def memory_search_probe(*, uri: str, embedding_name: str) -> DaemonProbe:
    try:
        return search_verdict(post_search(uri, embedding_name=embedding_name))
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return DaemonProbe.down(f"protocol probe failed: {exc}")
