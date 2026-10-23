from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import replace
from typing import Any

from fastapi.testclient import TestClient

from gateway.tests.auth.gateway_config import (
    GatewayTestClient,
    gateway_test_client,
    gateway_test_config,
)

ACCEPT = "application/json, text/event-stream"


def sse_messages(body: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for event in body.replace("\r\n", "\n").split("\n\n"):
        data = [line[5:].strip() for line in event.split("\n") if line.startswith("data:")]
        if data:
            out.append(json.loads("\n".join(data)))
    return out


def post_mcp(
    client: TestClient, payload: dict[str, Any], headers: Mapping[str, str] | None = None
) -> Any:
    return client.post(
        "/mcp",
        json=payload,
        headers={"Accept": ACCEPT, "Content-Type": "application/json", **(headers or {})},
    )


def post_message(
    client: TestClient, payload: dict[str, Any], headers: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    resp = post_mcp(client, payload, headers)
    assert resp.status_code == 200, f"history={resp.history!r} body={resp.text}"
    return sse_messages(resp.text)


def initialize(client: TestClient) -> list[dict[str, Any]]:
    return post_message(
        client,
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
    )


def tool_call(
    client: TestClient,
    name: str,
    args: dict[str, Any],
    req_id: int = 1,
    headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    messages = post_message(
        client,
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
        headers,
    )
    assert len(messages) == 1
    return messages[0]


def tool_result(message: dict[str, Any]) -> Any:
    if message["result"].get("isError"):
        return message["result"]["content"][0]["text"]
    structured: dict[str, Any] = message["result"]["structuredContent"]
    if list(structured) == ["result"]:
        return structured["result"]
    return structured


# meiva:note
# A gateway test client whose app serves (or, with `enabled=False`, refuses) `/mcp`.
#
# The endpoint switch is a `GatewayConfig` value the app is built with; `client_options` are
# the usual `gateway_test_client` arguments. A `config` among them keeps its other fields.
def mcp_client(
    *, enabled: bool = True, **client_options: Any
) -> AbstractContextManager[GatewayTestClient, bool | None]:
    config = client_options.pop("config", None) or gateway_test_config()
    return gateway_test_client(
        config=replace(config, mcp_endpoint_enabled=enabled), **client_options
    )
