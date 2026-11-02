---
type: doc
title: Gateway — same-origin browser entries
description: MEIVA_BROWSER_ORIGINS, the list of same-origin HTTPS entries, and what each decision that reads it does — startup validation, page routing, CSP, session cookie, CORS, trusted hosts and the primary entry.
tags: []
---

# Gateway — same-origin browser entries (`MEIVA_BROWSER_ORIGINS`)

A cluster can serve its console through several same-origin entries at once, for example a
private-network name and a public name. Each entry is a reverse proxy that sends `/api` (and
`/pages`, `/grafana`) to the gateway and everything else to Gate. `MEIVA_BROWSER_ORIGINS` lists them
as comma-separated `https://host[:port]` origins; `base/host/net/browser_origins.py` parses the list
once and owns every membership question:

- **Validation**: each entry must be a plain HTTPS origin (no credentials, path, query, fragment or
  empty port; the host a DNS name or IPv6 literal). The host is lowercased and a default `:443`
  dropped. A bad entry, an empty entry or a repeated origin refuses the process's startup; nothing is
  repaired.
- **Same-origin test**: a page is an entry when its origin is in the list. The Gate login page, the
  frontend transport and the frontend CSP (`connect-src`) apply the same test over the same list, so
  a page on any entry talks to its own origin, and a page elsewhere (a direct `host:port` URL) keeps
  its gateway-port routing.
- **Session cookie**: `session_cookie_secure` is true for a request whose scheme, host and port match
  any entry, and otherwise follows `MEIVA_GATEWAY_SESSION_COOKIE_SECURE` or the gateway URL's scheme.
  The cookie is host-only, so a login on one entry does not carry to another.
- **CORS and trusted hosts**: when `MEIVA_GATEWAY_CORS_ALLOWED_ORIGINS` is empty every entry joins the
  derived allowlist, and with it the hosts an anonymous request may name. An explicit allowlist stays
  authoritative and must list each entry itself.
- **Primary entry**: the first entry stands for the cluster where one address is needed (the login link
  `meiva start` prints).

Decision: `docs/decisions/data/security/2026-11-03-multiple-browser-origins.md`.
