# Optional HTTPS browser entries (HTTP/2)

Use a browser-facing HTTPS reverse proxy when several visible console windows
exhaust HTTP/1.1's six connections with SSE. Keep `MEIVA_GATEWAY_URL` and the
existing listener ports as the runner/control-plane addresses. On the gateway
unit, set `MEIVA_BROWSER_ORIGINS=https://<entry-host>[,https://<other-host>...]`
(comma-separated origins only, no path; the first one is the primary entry, the one
`meiva start` prints in its login link). Several entries serve one cluster at once,
for example a private-network name and a public name, each its own reverse proxy
with the same routes. An invalid or repeated entry refuses startup.
The normal frontend build derives its browser setting from this field; do not
write `NEXT_PUBLIC_*` overrides. Deploy the change through the normal update
path so the gateway, gate login page and rebuilt frontend agree. If an explicit
`MEIVA_GATEWAY_CORS_ALLOWED_ORIGINS` list exists, add every exact new origin there;
the explicit allowlist remains authoritative.

This setting currently requires a source-built frontend. Retained standalone
images do not record a browser origin in their build manifest; their launcher
refuses a nonempty setting instead of silently serving a mismatched bundle.

Only a browser visiting one of those exact origins uses same-origin API/SSE. Direct IP
frontend URLs keep their existing gateway-port routing for staged verification
and rollback. Login through an HTTPS entry sets a Secure session cookie;
direct HTTP login retains its existing policy. The cookie is host-only: a
hostname change, or a visit to another entry, requires a new browser login. Forward the original Host and overwrite X-Forwarded-Host /
X-Forwarded-Proto at the trusted entry; Uvicorn's trusted proxy boundary stays
loopback, never `forwarded-allow-ips=*`.

Route `/api` and its descendants, `/pages` and its descendants, and `/grafana`
and its descendants directly to the gateway; all remaining requests go to the
existing gate. The gate buffers frontend
responses and must not proxy SSE. Preserve route prefixes, query strings,
cookies, redirects, and immediate `text/event-stream` delivery. Keep the entry
supervised independently of rollout service teardown.

If the host's private network already provides a managed HTTPS entry (listener,
certificate renewal and persistence), it can take this role without a second
proxy daemon. Keep that entry reachable only on the private network, never
publicly exposed. If its certificates come from a public CA, issuance
publishes the node's full DNS name in Certificate Transparency. Save the
entry's current configuration first and refuse to overwrite an occupied HTTPS
port. For example, for gateway 20016 and gate 20017 on an otherwise unused
HTTPS port 443, add exactly four handlers:

| Path on each `https://<entry-host>` | Upstream |
|---|---|
| `/api` | `http://127.0.0.1:20016/api` |
| `/pages` | `http://127.0.0.1:20016/pages` |
| `/grafana` | `http://127.0.0.1:20016/grafana` |
| `/` (everything else) | `http://127.0.0.1:20017` |

An entry that strips the mount prefix needs the upstream URL to restore it, as
above. Make the handlers persist across restarts of the entry. Preserve its
other handlers and TCP relays; never reset the whole entry to roll this back.
Remove only the four added handlers. The direct IP entry remains available
throughout.

Acceptance: verify ALPN negotiates `h2` from the user's machine with normal
certificate verification; load several console windows and inspect the real
browser Network protocol and queueing; check login, tree, inspector, SSE,
Grafana/pages and maintenance-state requests. API headers alone are not proof
that the browser used HTTP/2. Full test suites run in CI; local verification
uses the affected tests and the operator's authorized browser smoke.
