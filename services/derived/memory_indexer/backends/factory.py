"""One switch selects the memory search storage backend.

`get_backend()` is the single entry point: the indexer daemon (write
path) and the gateway search endpoint (read path) both take their backend
from here, keyed by the name supplied by the composition root
(`MEIVA_MEMORY_SEARCH_BACKEND`, default `numpy`). Switching storage is one
env var + a restart; the cold-start reconcile rebuilds the index on the
new backend.

Each call returns a fresh, unconnected backend; the caller owns connect /
close (the daemon holds one for its lifetime; the gateway's async search
connects and closes per request).

Backend implementations are imported only when selected. Importing this
factory therefore stays independent of optional backend dependency trees and
keeps the daemon's cold-import path cheap.

The embedding vector space is injected at construction: callers pass the
provider's `dim` (schema width) and `fingerprint` (semantic-space
identifier stored per row) — a backend never imports a provider constant,
so changing embedding providers is a data change, not a code change.
"""

from __future__ import annotations

from collections.abc import Callable

from base.db import Database
from services.derived.memory_indexer.backends.base import MemorySearchBackend


def _numpy_backend(
    database: Database, dim: int, fingerprint: str, *, uri: str, readonly: bool
) -> MemorySearchBackend:
    # meiva:note
    # NumPyBackend is a thin HTTP client — the vector space lives in the
    # memory_search service process, so database/dim are accepted for the
    # uniform factory signature only. The fingerprint guards every HTTP operation.
    from services.derived.memory_indexer.backends.numpy import NumPyBackend

    del database, dim
    return NumPyBackend(uri=uri, fingerprint=fingerprint, readonly=readonly)


def _pgvector_backend(
    database: Database, dim: int, fingerprint: str, *, uri: str, readonly: bool
) -> MemorySearchBackend:
    del uri  # This backend has no HTTP endpoint.
    from services.derived.memory_indexer.backends.pgvector import PGVectorBackend

    return PGVectorBackend(database, dim=dim, fingerprint=fingerprint, readonly=readonly)


# meiva:note
# Uniform constructor shape `(database, dim, fingerprint, uri, readonly)`.
BACKENDS: dict[str, Callable[[Database, int, str, str, bool], MemorySearchBackend]] = {
    "numpy": lambda database, dim, fingerprint, uri, readonly: _numpy_backend(
        database, dim, fingerprint, uri=uri, readonly=readonly
    ),
    "pgvector": lambda database, dim, fingerprint, uri, readonly: _pgvector_backend(
        database, dim, fingerprint, uri=uri, readonly=readonly
    ),
}


# meiva:note
# Construct the backend `name` selects (the root read it from
# `MEIVA_MEMORY_SEARCH_BACKEND`) for the given embedding vector space; unknown names
# fail fast (an unrecognized value must not silently fall back to numpy: a typo would
# otherwise keep the old storage while the operator believes the switch happened).
def get_backend(
    name: str,
    *,
    database: Database,
    dim: int,
    fingerprint: str,
    uri: str,
    readonly: bool = False,
) -> MemorySearchBackend:
    try:
        ctor = BACKENDS[name]
    except KeyError:
        known = ", ".join(sorted(BACKENDS))
        raise ValueError(f"unknown memory search backend {name!r} (known: {known})") from None
    return ctor(database, dim, fingerprint, uri, readonly)
