# The `/mcp` endpoint is a thin forwarder onto the gateway HTTP API

## Context

The gateway `/mcp` endpoint (2026-08-25, [MCP client credentials](2026-08-25-mcp-client-credentials.md))
grew its own copy of what the HTTP API already owns: a token table (`mcp_clients`) with
`read`/`write` scopes and a credential-creation receipt, a bearer check that bypassed
`cluster_auth_middleware`, an audit event with redacted arguments, identity-argument validation,
and tool bodies that called the domain functions in process rather than the API routes. Every
entry point to the same capability had its own authentication, permission model and behaviour, so
a rule fixed in the API (an error shape, a validation, an idempotency key) had to be fixed again
in the MCP tool.

On 2026-10-20 the user ruled that MCP and the CLI are thin wrappers over the gateway HTTP API.
They only forward and provide nothing new: no authentication of their own, no read/write
permission, no separate audit or validation. Logic worth keeping belongs to the API layer; the
rest is deleted.

## Decision

- `/mcp` is authenticated by `cluster_auth_middleware` like every API route: a session cookie, the
  cluster bearer, a machine token, or no credential in `trusted_network` mode (with its Origin and
  Host checks). `/mcp` is no longer in the auth bypass list.
- Each tool is one HTTP request to an existing API route, sent to the same app in process through
  an ASGI transport. It carries the caller's own `Authorization`, `Cookie`, `Host` and `Origin`, so
  the API authenticates, validates, applies idempotency and records the request exactly as it
  would for the caller directly. The tool returns the response JSON unchanged; an API error becomes
  a `ToolError` whose text is the API's error envelope.
- The MCP client token system is deleted: `mcp_clients`, the credential-creation receipts, the
  `/api/mcp/clients` and `/api/keyed/v1/mcp/clients` routes, `McpClientScope`, the per-tool write
  check, the `mcp_tool_call` audit event and `AuthPrincipal` kind `mcp_client`.
- `require_human_credential` stays: login tickets still mint a credential that outlives a write
  generation.

## Alternatives rejected

- **Keep client tokens as a second credential type.** It preserves per-client revocation, scope
  and audit identity, but only by keeping a second authentication system that every future
  auth change must also reach, for a capability the API already exposes.
- **Keep the tools calling domain functions, only moving authentication.** The behaviour would
  still be duplicated (argument shapes, error text, idempotency scoping) and drift from the API.
- **Dial the gateway over a real socket.** An in-process ASGI call reaches the same middleware and
  routes with no network hop, no port or TLS question and no second listener.

## Consequences

- External MCP clients present the cluster bearer (or run against a `trusted_network` listener).
  They cannot be revoked individually, cannot be limited to read-only, and appear in audit and
  idempotency scopes as the cluster administrator, not as a named client. Existing client tokens
  stop working when the migration drops the tables.
- `send_message` takes a `source` argument (default `user`) and `spawn_agent_guarded_v1` takes
  `prompt_source` and `spawner`; all pass to the API unchanged and the API validates them, so a
  caller declares its own `external_agent:<subject>[:<instance>]`. There is no server-derived
  client instance any more.
- `get_messages` returns the API's raw message window (`messages`, `msg_count`, `start_index`,
  `has_more`) instead of the former role/text/code projection.
- A change to an API route is a change to its MCP tool; the tool surface stays seven tools.

<!-- Supersedes: decisions/data/security/2026-08-25-mcp-client-credentials.md -->
