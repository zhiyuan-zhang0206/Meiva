"""HTTPX public APIs remain usable after a fresh host-network import."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]


# meiva:note
# Child process runs this test module with a fixed argv and trusted interpreter to verify isolated import behavior.
def _probe(mode: str) -> dict[str, object]:
    proc = subprocess.run(  # noqa: S603 — fixed argv, sys.executable is trusted
        [sys.executable, "-I", "-X", "utf8", str(Path(__file__)), str(_REPO_ROOT), mode],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_httpx_sync_and_async_transports_after_host_network_import() -> None:
    report = _probe("transport")
    assert report["transport_ok"] is True


def test_host_network_import_preserves_httpx_cli_help() -> None:
    report = _probe("cli_help")
    assert report["exit_code"] == 0


def _run_child(repo_root: str, mode: str) -> None:
    sys.path.insert(0, repo_root)
    import asyncio
    from collections.abc import Callable
    from typing import cast

    import httpx

    import base.host.net as _host_net

    assert _host_net is not None
    if mode == "transport":

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"path": request.url.path})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            assert client.get("http://x.test/sync").json() == {"path": "/sync"}

        async def async_get() -> object:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return (await client.get("http://x.test/async")).json()

        assert asyncio.run(async_get()) == {"path": "/async"}
        sys.stdout.write(json.dumps({"transport_ok": True}) + "\n")
    elif mode == "cli_help":
        try:
            cast(Callable[..., object], httpx.main)(args=["--help"], prog_name="httpx")
        except SystemExit as error:
            sys.stdout.write(json.dumps({"exit_code": error.code}) + "\n")


if __name__ == "__main__":
    _run_child(sys.argv[1], sys.argv[2])
