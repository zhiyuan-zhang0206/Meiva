"""The memory-search composition root builds its slice from the flat registry fields."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import socket
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from base.config import ConfigBoot
from services.derived.memory_indexer.embeddings import factory
from services.derived.memory_search import daemon
from services.derived.memory_search.config import MemorySearchConfig

# meiva:note
# The slice field whose flat registry name differs from its own.
_FLAT_NAME = {"embedding_name": "embedding_backend"}


@pytest.fixture
def owned_config_environment() -> Iterator[None]:
    """Restore environment delivery and the process timezone after a real ConfigBoot."""
    try:
        with patch.dict(os.environ):
            yield
    finally:
        time.tzset()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.usefixtures("owned_config_environment")
def test_the_slice_carries_the_live_value_of_every_field() -> None:
    boot = ConfigBoot()
    config = daemon.memory_search_config(config=boot)
    for field in dataclasses.fields(config):
        flat = getattr(boot.view.services, _FLAT_NAME.get(field.name, field.name))
        assert getattr(config, field.name) == flat, field.name


async def test_storage_boot_serves_the_provider_width_over_a_real_socket(tmp_path: Path) -> None:
    descriptor = factory.get_descriptor("gemini")
    config = MemorySearchConfig(
        memory_search_pidfile=tmp_path / "memory-search.pid",
        memory_search_data_dir=tmp_path,
        memory_search_port=_free_port(),
        memory_search_max_batch_rows=10,
        embedding_name="gemini",
    )
    server = await daemon.build_server(config)
    serving = asyncio.create_task(server.serve())
    try:
        deadline = time.monotonic() + 10.0
        while not server.started:
            assert not serving.done(), "the server stopped before it started"
            assert time.monotonic() < deadline, "the server did not start"
            await asyncio.sleep(0.02)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{config.memory_search_port}"
        ) as client:
            schemas = (await client.get("/openapi.json")).json()["components"]["schemas"]
            meta = (await client.get("/meta")).json()
            too_wide = await client.post(
                "/search", json={"vector": [0.0] * (descriptor.dim + 1), "k": 1}
            )
    finally:
        server.should_exit = True
        await serving
    assert schemas["SearchBody"]["properties"]["vector"]["maxItems"] == descriptor.dim
    assert meta == {}
    assert too_wide.status_code == 422


async def test_storage_boot_rejects_an_unknown_provider_name(tmp_path: Path) -> None:
    config = MemorySearchConfig(
        memory_search_pidfile=tmp_path / "memory-search.pid",
        memory_search_data_dir=tmp_path,
        memory_search_port=_free_port(),
        memory_search_max_batch_rows=10,
        embedding_name="unknown-provider",
    )
    with pytest.raises(ValueError, match="unknown-provider"):
        await daemon.build_server(config)


def test_schema_vector_bound_belongs_to_each_app(tmp_path: Path) -> None:
    from services.derived.memory_search.app import build_app
    from services.derived.memory_search.store import MemoryStore

    for dim in (3, 7):
        app = build_app(
            MemoryStore(tmp_path / f"vectors-{dim}.npz", dim=dim, fingerprint=f"test:{dim}"),
            10,
            embedding_dim=dim,
        )
        schemas = app.openapi()["components"]["schemas"]
        for name in ("UpsertBody", "SearchBody"):
            assert schemas[name]["properties"]["vector"]["maxItems"] == dim
