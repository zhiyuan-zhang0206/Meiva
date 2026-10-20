from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast

import httpx
from fastapi import FastAPI, Request
from mcp.server.mcpserver import Context

from base.api_contracts.idempotency import keyed_headers
from base.api_contracts.mcp_tool_contract import server_instructions, tool_description
from gateway.http.middleware.error_envelope import error_response

# meiva:note
# The request headers a forwarded call inherits: the credentials the auth middleware reads,
# plus the Host and Origin it checks on a trusted network.
# meiva:link gateway/mcp_server/docs/mcp-endpoint/mcp-endpoint.meiva.okf.md
_FORWARDED_HEADERS = ("authorization", "cookie", "host", "origin")
_API_BASE_URL = "http://mcp-forward.invalid"
_SPAWNER = "mcp"
_MESSAGE_SOURCE = "user"


async def _call_api(
    app: FastAPI,
    ctx: Context,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    from mcp.server.mcpserver.exceptions import ToolError

    origin = ctx.request_context.request
    if not isinstance(origin, Request):
        raise TypeError("an MCP tool call must arrive over an HTTP request")
    forwarded = {
        name: origin.headers[name] for name in _FORWARDED_HEADERS if name in origin.headers
    }
    peer = ("", 0) if origin.client is None else (origin.client.host, origin.client.port)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=peer), base_url=_API_BASE_URL
    ) as api:
        response = await api.request(
            method,
            path,
            params={k: v for k, v in (params or {}).items() if v is not None},
            json=body,
            headers={**forwarded, **(headers or {})},
        )
    if response.is_error:
        raise ToolError(response.text)
    return cast(dict[str, Any], response.json())


def _register_tools(server: object, app: FastAPI) -> None:
    from mcp.server.mcpserver import MCPServer

    typed_server = cast(MCPServer, server)

    @typed_server.tool(description=tool_description("list_agents"))
    async def list_agents(
        ctx: Context,
        scope: Literal["live", "terminated", "all"] | None = None,
        query: str | None = None,
        before_id: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params = {"scope": scope, "query": query, "before_id": before_id, "limit": limit}
        return await _call_api(app, ctx, "GET", "/api/agents", params=params)

    @typed_server.tool(description=tool_description("get_agent"))
    async def get_agent(agent_id: int, ctx: Context) -> dict[str, Any]:
        return await _call_api(app, ctx, "GET", f"/api/agents/{agent_id}")

    @typed_server.tool(description=tool_description("cluster_status"))
    async def cluster_status(ctx: Context) -> dict[str, Any]:
        return await _call_api(app, ctx, "GET", "/api/cluster/status")

    @typed_server.tool(description=tool_description("spawn_agent_guarded_v1"))
    async def spawn_agent_guarded_v1(
        prompt: str,
        idempotency_key: str,
        ctx: Context,
        label: str | None = None,
        machine: str | None = None,
        config_overlay: dict[str, Any] | None = None,
        prompt_source: str = _MESSAGE_SOURCE,
        spawner: str = _SPAWNER,
    ) -> dict[str, Any]:
        body = {
            "prompt": prompt,
            "prompt_source": prompt_source,
            "spawner": spawner,
            "label": label,
            "machine": machine,
            "config": config_overlay,
        }
        return await _call_api(
            app,
            ctx,
            "POST",
            "/api/keyed/v1/agents",
            body=body,
            headers=keyed_headers(idempotency_key),
        )

    @typed_server.tool(description=tool_description("send_message"))
    async def send_message(
        agent_id: int,
        content: str,
        ctx: Context,
        *,
        source: str = _MESSAGE_SOURCE,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return await _call_api(
            app,
            ctx,
            "POST",
            f"/api/agents/{agent_id}/messages",
            body={"content": content, "source": source},
            headers=None if idempotency_key is None else keyed_headers(idempotency_key),
        )

    @typed_server.tool(description=tool_description("get_messages"))
    async def get_messages(agent_id: int, ctx: Context, limit: int | None = None) -> dict[str, Any]:
        return await _call_api(
            app, ctx, "GET", f"/api/agents/{agent_id}/messages", params={"limit": limit}
        )

    @typed_server.tool(description=tool_description("terminate_agent"))
    async def terminate_agent(
        agent_id: int,
        ctx: Context,
        *,
        message: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        return await _call_api(
            app,
            ctx,
            "POST",
            f"/api/agents/{agent_id}/terminate",
            body={"message": message, "force": force},
        )


# meiva:link gateway/mcp_server/docs/mcp-endpoint/model-capability-failures.meiva.okf.md
def build_manager(app: FastAPI):  # noqa: ANN201
    from mcp.server.mcpserver import MCPServer
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    server = MCPServer("meiva", instructions=server_instructions())
    _register_tools(server, app)
    server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, host="")
    manager: StreamableHTTPSessionManager = server.session_manager
    return manager


def mcp_gateway(app: FastAPI) -> Callable[..., Awaitable[None]]:

    async def _gateway(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return
        manager = getattr(app.state, "mcp_manager", None)
        if manager is None:
            response = error_response(
                Request(scope),
                code="mcp_endpoint_disabled",
                status=404,
                detail="mcp endpoint is disabled",
                retryable=False,
            )
            await response(scope, receive, send)
            return
        await manager.asgi_app(scope, receive, send)

    return _gateway
