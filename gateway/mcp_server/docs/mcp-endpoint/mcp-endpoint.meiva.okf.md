---
type: doc
title: Gateway /mcp endpoint — a thin forwarder onto the HTTP API
description: 'Gateway MCP control tools that forward each call to an API route in process, authenticated by the API middleware; stateless transport.'
tags:
- gateway
- mcp
- tool
---

# Gateway `/mcp` endpoint

## What it is

One Streamable HTTP MCP endpoint on the gateway that external MCP clients (Claude Code /
Codex / anything) dial to drive the fleet — the same control effects the web UI and the
`meiva` CLI reach over the HTTP API. It is a **thin forwarder**
([decision](../../../../docs/decisions/data/security/2026-10-20-mcp-is-a-thin-api-forwarder.md)):
it has no authentication, permission model, validation or audit of its own, and no business
logic. Every tool is one request to an existing API route; what the route decides is what the
tool returns.

## Tool to route map

| Tool | Request |
|---|---|
| `list_agents` | `GET /api/agents` (`scope`, `query`, `before_id`, `limit` as query parameters) |
| `get_agent` | `GET /api/agents/{agent_id}` |
| `cluster_status` | `GET /api/cluster/status` |
| `spawn_agent_guarded_v1` | `POST /api/keyed/v1/agents` (`Idempotency-Key` from `idempotency_key`; body `prompt`, `prompt_source` (default `user`), `spawner` (default `mcp`), `label`, `machine`, `config=config_overlay`) |
| `send_message` | `POST /api/agents/{agent_id}/messages` (body `content`, `source`, default `user`, passed unchanged and validated by the API; optional `Idempotency-Key`) |
| `get_messages` | `GET /api/agents/{agent_id}/messages` (`limit`) |
| `terminate_agent` | `POST /api/agents/{agent_id}/terminate` (body `message`, `force`) |

Keyed requests also carry `Idempotency-Scope: principal-v1`. Omitted optional arguments are not
sent, so the route owns every default.

## Mechanics

- **Flag-gated, additive**: `settings.gateway.mcp_endpoint_enabled`
  (MEIVA_MCP_ENDPOINT_ENABLED), default off. Off, `/mcp` answers 404 via the
  `mcp_gateway` ASGI wrapper; the existing mcp-daemon path is untouched either way.
- **Mount**: `app.mount("/mcp", mcp_gateway(app))` — the wrapper reads
  `app.state.mcp_manager`, set by the gateway lifespan, which builds a fresh
  `StreamableHTTPSessionManager` per lifespan entry and enters `manager.run()` (it can only run
  once per instance).
- **Auth**: `/mcp` is not bypassed: `cluster_auth_middleware` admits it like any API route
  (session cookie, cluster bearer, machine token, or no credential under
  `gateway.human_auth = trusted_network` with that mode's Origin and Host checks). Otherwise 401.
- **Forwarding**: a tool sends its request to the same app through `httpx.ASGITransport`,
  carrying the caller's `Authorization`, `Cookie`, `Host` and `Origin` headers. The middleware
  therefore authenticates the forwarded request again with the caller's own credential (the Host
  check of `trusted_network` sees the original Host), and the route sees the same principal and
  credential fact it would for the caller directly. Tool arguments and `clientInfo` never supply
  authority.
- **Errors**: a non-2xx API response becomes a `ToolError` whose text is the API's error envelope
  (JSON: `code`, `status`, `detail`, ...); a tool-argument schema violation is rejected by the MCP
  SDK before any request is made.
- **Transport**: stateless Streamable HTTP — one fresh transport per POST, no server-side
  session state, no idle reaping. `host=""` skips the SDK's loopback-only DNS-rebinding
  auto-guard (the middleware's Host check applies instead).
- **Advertised contract**: `base/api_contracts/mcp_tool_contract.py` owns the advertised
  instructions and the seven tool descriptions; the tool signatures generate the input schemas.

`spawn_agent_guarded_v1` requires a caller `idempotency_key`; creation, replay and conflicts are
the route's. Clients keep the same key and arguments across retries of one intent.

## Why not a router

`/mcp` is not a FastAPI router: the MCP protocol is JSON-RPC over HTTP with its
own lifecycle (initialize handshake), served by the mcp SDK's ASGI app — so it
mounts as a raw ASGI wrapper rather than joining the `/api/*` router set.

## Model capability failures

[[gateway/mcp_server/docs/mcp-endpoint/model-capability-failures.meiva.okf.md|MCP endpoint model capability failures]] — which tools keep working without the catalog, and the disabled endpoint's lazy-SDK-types invariant.
