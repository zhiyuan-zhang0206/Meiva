from __future__ import annotations

import socket

import pytest

from base.deploy.git import memory_repo
from base.deploy.git.tests.pool_server import PoolResponse, pool_server


def test_download_pool_snapshot_streams_and_enforces_cap_midstream() -> None:
    with (
        pool_server(PoolResponse(b"", chunks=(b"x" * 8, b"x" * 8, b"x" * 8))) as url,
        pytest.raises(memory_repo.MemoryPoolBootstrapFailed, match="exceeds"),
    ):
        memory_repo.download_pool_snapshot(f"{url}/api/memory/pool", {}, max_bytes=16)


def test_download_pool_snapshot_declared_oversize_rejected_before_body() -> None:
    # meiva:note
    # A body read would fail because the declared bytes are not sent. The cap error
    # instead of a transport error proves rejection precedes response iteration.
    with (
        pool_server(PoolResponse(b"", declared_length="999999999")) as url,
        pytest.raises(memory_repo.MemoryPoolBootstrapFailed, match="declared"),
    ):
        memory_repo.download_pool_snapshot(f"{url}/api/memory/pool", {}, max_bytes=16)


def test_download_pool_snapshot_transport_error_wrapped() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{listener.getsockname()[1]}/api/memory/pool"
        with pytest.raises(memory_repo.MemoryPoolBootstrapFailed, match="failed:"):
            memory_repo.download_pool_snapshot(url, {})


def test_invalid_wire_content_length_is_a_transport_failure() -> None:
    with (
        pool_server(PoolResponse(b"", declared_length="invalid")) as url,
        pytest.raises(memory_repo.MemoryPoolBootstrapFailed, match="failed:"),
    ):
        memory_repo.download_pool_snapshot(f"{url}/api/memory/pool", {})
