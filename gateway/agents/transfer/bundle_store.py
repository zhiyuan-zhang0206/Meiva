# meiva:note
# Request-scoped staging store for in-flight workspace transfer bundles on `app.state`.
#
# Lives on `FastAPI.state.agent_transfer_bundles` (no module-level mutable state). Each transfer
# stages its verified `MEIVA_WS_V1` bytes inside a `with store.stage(transfer_id, bundle_bytes):`
# block for the duration of the `workspace_transfer_import` RPC and removes them unconditionally on
# scope exit.
# meiva:link gateway/agents/docs/agent-transfer.meiva.okf.md

from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager


class TransferBundleStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bundles: dict[str, bytes] = {}

    @contextmanager
    def stage(self, transfer_id: str, bundle_bytes: bytes) -> Generator[None]:
        with self._lock:
            if transfer_id in self._bundles:
                raise RuntimeError(f"duplicate in-flight transfer_id: {transfer_id}")
            self._bundles[transfer_id] = bundle_bytes
        try:
            yield
        finally:
            with self._lock:
                self._bundles.pop(transfer_id, None)

    def get(self, transfer_id: str) -> bytes | None:
        with self._lock:
            return self._bundles.get(transfer_id)
