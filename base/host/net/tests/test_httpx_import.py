"""HTTPX public APIs remain usable after a fresh host-network import."""

from __future__ import annotations

from base.host.net.tests.resilience_process import report


def test_httpx_sync_and_async_transports_after_host_network_import() -> None:
    assert report("transports")["transport_ok"] is True


def test_host_network_import_preserves_httpx_cli_help() -> None:
    assert report("cli-help")["exit_code"] == 0
