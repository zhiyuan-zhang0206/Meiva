# meiva:note
# A real memory-search service for tests: the production app and store on a loopback port.
#
# The indexer's NumPy backend, the reconcile tool and the gateway dial this service over HTTP, so
# their tests run against a listener rather than a stand-in.

from __future__ import annotations

import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from base.host.env import port_lease
from services.derived.memory_search.app import build_app
from services.derived.memory_search.store import MemoryStore


# meiva:note
# One port this session holds, from the host-wide lease: the number is refused to
# every other live holder on the host, so two workers — or two runs of one shard on
# one machine — cannot be given it.
def free_port() -> int:
    return port_lease.lease_ports(1)[0]


# meiva:note
# Serve a store persisted at `data_file` and yield the service's URI; stop it on exit.
@contextmanager
def serve_memory_search(
    data_file: Path, *, dim: int, fingerprint: str, port: int | None = None
) -> Generator[str]:
    listen_port = free_port() if port is None else port
    server = uvicorn.Server(
        uvicorn.Config(
            build_app(
                MemoryStore(data_file, dim=dim, fingerprint=fingerprint), 500, embedding_dim=dim
            ),
            host="127.0.0.1",
            port=listen_port,
            log_level="warning",
            access_log=False,
            log_config=None,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("memory_search test server failed to start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{listen_port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
