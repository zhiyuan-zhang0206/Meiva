# meiva:link meiva_builtins/mcps/docs/chrome/chrome.md

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path

import httpx

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from services.desktop.browser.routing.mcp_socket_bridge import LINE_LIMIT, SocketLink

BROWSER_RPC_ROUTE = ("POST", "/browser/rpc")
RPC_TIMEOUT_S = 180.0


async def call_browser_socket(
    path: Path, request: dict[str, object], *, timeout_s: float = RPC_TIMEOUT_S
) -> object:
    async with asyncio.timeout(timeout_s):
        reader, writer = await asyncio.open_unix_connection(path, limit=LINE_LIMIT)
        link = SocketLink(
            reader,
            writer,
            service_label="browser-mcp",
            timeout_reader=lambda: timeout_s,
            extra_fields={},
        )
        try:
            return await link.request(request)
        finally:
            link.close()
            with suppress(OSError):
                await writer.wait_closed()


async def call_browser_peer(
    url: str,
    headers: Mapping[str, str],
    body: Mapping[str, object],
    *,
    timeout_s: float,
) -> object:
    endpoint = url.rstrip("/") + BROWSER_RPC_ROUTE[1]
    try:
        async with (
            asyncio.timeout(timeout_s),
            httpx.AsyncClient(trust_env=False, timeout=timeout_s) as client,
            client.stream("POST", endpoint, headers=headers, json=body) as response,
        ):
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > LINE_LIMIT:
                    raise ValueError("Browser peer reply exceeds the MCP response size limit")
                data.extend(chunk)
            raw = object_of(json.loads, data)
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"Browser peer returned HTTP {exc.response.status_code}") from exc
    except (httpx.RequestError, TimeoutError) as exc:
        raise RuntimeError(f"Browser peer did not return a result ({type(exc).__name__})") from exc
    if not is_object_dict(raw) or raw.get("ok") is not True:
        message = raw.get("error") if is_object_dict(raw) else None
        raise RuntimeError(str(message or "Invalid reply from browser peer"))
    return raw.get("result")


def probe_browser_peer(url: str, *, headers: Mapping[str, str]) -> bool | None:
    try:
        with httpx.Client(trust_env=False, timeout=2.0) as client:
            response = client.post(
                url.rstrip("/") + BROWSER_RPC_ROUTE[1], headers=headers, json={"method": "status"}
            )
            response.raise_for_status()
            parsed = object_of(response.json)
    except (httpx.HTTPError, ValueError):
        return None
    if is_object_dict(parsed) and parsed.get("ok") is True:
        ready = parsed.get("result")
        return ready if isinstance(ready, bool) else None
    return None
