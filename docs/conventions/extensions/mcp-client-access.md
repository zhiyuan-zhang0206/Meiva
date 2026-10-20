# Connecting external MCP clients to the gateway `/mcp` endpoint

The gateway exposes the cluster control plane as a standard MCP server over
[Streamable HTTP] at `/mcp` (implementation
[`gateway/mcp_server/endpoint.py`](../../../gateway/mcp_server/docs/mcp-endpoint/mcp-endpoint.meiva.okf.md)). Any MCP
client — Claude Code, Codex, anything speaking the protocol — drives the fleet
through it with seven control tools
(list_agents / get_agent / spawn_agent_guarded_v1 / send_message / get_messages /
terminate_agent / cluster_status). Each tool forwards to the matching HTTP API
route, so `/mcp` has no credentials, scopes or audit of its own: it accepts what the
API accepts.

[Streamable HTTP]: https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#streamable-http

## Enable the endpoint

The endpoint ships **off** — flag-gated so a cluster that does not use it is
completely unaffected (off, `/mcp` answers 404):

```bash
meiva config set MEIVA_MCP_ENDPOINT_ENABLED true
# then restart the gateway: the flag is read at gateway start
```

The flag is an ordinary cluster-pinned setting (config API, `meiva config set`, or the
gateway `.env`); `/mcp` adds no authority beyond the API.

## Connect a client

Present the same credential the API takes: the cluster secret as a bearer. On a
cluster running `gateway.human_auth = trusted_network` a client on the trusted
network needs no credential at all (its `Host` must be one the gateway trusts).

```bash
claude mcp add --transport http meiva http://<gateway-host>/mcp \
  --header "Authorization: Bearer <cluster-secret>"
```

Codex takes the same shape (`codex mcp add`). `<gateway-host>` is the address
the cluster advertises (`MEIVA_GATEWAY_URL`). A client cannot be revoked or
limited to read-only on its own: it is the cluster administrator, exactly as for
the API. To cut one off, rotate the secret it holds.

## Verify

```bash
curl -s http://<gateway-host>/mcp \
  -H "Authorization: Bearer <cluster-secret>" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

A healthy endpoint answers an SSE stream whose single `data:` frame carries the
seven tools. A cluster with the flag off gets 404; a missing or invalid
credential gets 401 (the API's error envelope).

## Notes

- **Stateless**: each POST is one self-contained JSON-RPC message; the endpoint
  keeps no server-side session state, so clients must not rely on
  `Mcp-Session-Id` resumability.
- **Errors**: a failed call returns a tool error whose text is the API's error
  envelope (`code`, `status`, `detail`).

Agent creation uses `spawn_agent_guarded_v1` with a required 1–128 character
`idempotency_key`. Retain the same key and arguments to recover the original
birth after response loss. See the endpoint owner for the tool-to-route map.
