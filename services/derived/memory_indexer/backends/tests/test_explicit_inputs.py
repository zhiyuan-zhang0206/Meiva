from __future__ import annotations

import httpx
import numpy as np

from services.derived.memory_indexer.backends.numpy import NumPyBackend


async def test_uri_readers_are_lazy_separate_and_live_per_async_search() -> None:
    uris = {"one": "http://one", "two": "http://two"}
    reads: list[str] = []
    requests: list[str] = []
    client = httpx.AsyncClient

    def reader(name: str) -> str:
        reads.append(name)
        return uris[name]

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, json={"hits": []})

    def transport_client(*, base_url: str, timeout: float) -> httpx.AsyncClient:
        return client(base_url=base_url, timeout=timeout, transport=httpx.MockTransport(respond))

    one = NumPyBackend(uri_reader=lambda: reader("one"), async_client_factory=transport_client)
    two = NumPyBackend(uri_reader=lambda: reader("two"), async_client_factory=transport_client)
    assert reads == []
    vector = np.zeros(3, dtype=np.float32)
    await one.search_topk_async(vector, 1, timeout=1)
    await two.search_topk_async(vector, 1, timeout=1)
    uris["one"] = "http://updated"
    await one.search_topk_async(vector, 1, timeout=1)
    assert reads == ["one", "two", "one"]
    assert requests == ["http://one/search", "http://two/search", "http://updated/search"]
