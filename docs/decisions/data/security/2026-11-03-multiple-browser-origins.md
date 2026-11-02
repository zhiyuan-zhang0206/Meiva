# A cluster serves several same-origin browser entries

## Context

`MEIVA_BROWSER_ORIGIN` (2026-09-16, [an optional browser entry for HTTP/2](../../frontend/browser/2026-09-16-browser-http2-entry.md))
named one HTTPS origin behind which a reverse proxy serves the frontend and the gateway under one
host: `/api`, `/pages` and `/grafana` go to the gateway, everything else to Gate. A page whose origin
equals that value talks to its own origin; any other page dials `<page host>:<gateway port>`.

One entry is not enough once a console is reached by two names, for example a private-network name for
the operator's machines and a public name for a phone. Both names put the same two routes in front of
the same listeners, but the single value could cover only one of them. On the other name every
decision that compares the page with the value said "not an entry": the Gate login page posted to the
gateway's internal `http://<ip>:<port>` address, which a public HTTPS page cannot reach ("Connection
failed", and mixed content), the frontend dialed the same internal address, the CSP `connect-src` did
not match the origin the page talked to, and the session cookie lost `Secure`.

The decisions that read the value are spread over three runtimes: Python (the gateway's CORS
derivation, trusted hosts and cookie policy, the CLI's login link, the frontend build command), the
Gate login page (a static HTML page with an inline script) and the Next.js frontend (the transport and
the CSP, with the value inlined at build time).

## Decision

- The setting is a list, `MEIVA_BROWSER_ORIGINS` (`browser_origins`, `NEXT_PUBLIC_BROWSER_ORIGINS`):
  comma-separated `https://host[:port]` origins. The name is plural because the value is; the old
  singular key is removed everywhere, with no alias. A stale `MEIVA_BROWSER_ORIGIN` in an `.env` is
  ignored like any unknown key, so the operator sets the new key when deploying.
- Each entry must be a plain HTTPS origin: no credentials, path, query, fragment or empty port, a DNS
  name or IPv6 literal for host. A trailing slash and a default port are formatting and are normalized
  (the existing contract); anything else, an empty entry or a repeated origin refuses the process's
  startup. Nothing is repaired.
- "Same-origin entry" has one definition: the page's origin is in the list. The Python side owns it as
  pure functions in `base/host/net/browser_origins.py` (a `strict` module: parse, canonical origin of a
  URL, membership). The Gate login page and `gateway-origin.ts` repeat the one-line membership test over
  the same canonical list, which the Gate substitutes into the page and the build bakes into the
  bundle. The CSP follows from `gatewayOriginForRequest`: a page on an entry allows its own origin only.
- The session cookie is `Secure` for a request whose scheme, host and port match any entry. CORS
  derivation (when no explicit allowlist is set) adds every entry, and the trusted hosts an anonymous
  request may name follow from the allowed origins.
- The first entry is the primary entry, used where one address is needed (the login link `meiva start`
  prints). Order matters only there.
- The set of entries is part of the frontend build key: changing it rebuilds the frontend.

## Alternatives rejected

- **Keep the singular key and put a comma list in it.** It avoids a rename but leaves a key whose
  name says one and whose value is many, and an operator who writes the list before the code is
  deployed makes the old gateway fail to start on the old validator. The new key is inert on the old
  code, so the rollout can write it first.
- **A second setting for the second entry (`MEIVA_BROWSER_ORIGIN_2`).** It caps the number of entries
  at the number of names someone thought of, and every consumer then reads two values and must agree
  on which is primary.
- **Decide same-origin from the request's `Host` or `X-Forwarded-Host` at runtime, with no list.** The
  entry is a trust boundary: it selects a Secure cookie, a CSP and the CORS allowlist. Trusting a header
  that any direct client can send would let a direct visit claim an entry. The list is the operator's
  statement of which hosts are proxied entries.
- **Make the frontend ask the gateway at runtime which entries exist.** It adds a request before the
  first API call and a second source of the same fact beside the build; the build already receives the
  value for the CSP, which must be known when the first byte is served.
- **Allow HTTP entries.** The cookie policy marks an entry `Secure` unconditionally, so a plain-HTTP
  entry would lose its session. An HTTP page keeps the direct `host:port` routing.
- **Skip or repair a bad entry** (drop an empty element after a stray comma, strip a path). A
  misspelled entry would then fall back to the gateway-port routing without a message, which is the
  failure this change exists to remove.

## Consequences

- Production moves from one entry to two by writing
  `MEIVA_BROWSER_ORIGINS=<entry>,<entry>` in the gateway unit's `.env` and doing a full stop and start,
  so the frontend rebuilds with the list baked in.
- An explicit `MEIVA_GATEWAY_CORS_ALLOWED_ORIGINS` stays authoritative and must list every entry.
- The cookie is host-only, so a login on one entry does not carry to another; each entry signs in once.
- The Python, Gate-page and frontend implementations of the membership test cannot share code. Their
  agreement rests on the canonical form (what a browser serializes as the page's origin) and on tests of
  a page on the first entry, on the second entry and off the list in each runtime.
- Entries that differ only in a default port or in host case are the same origin and count as a
  repeat. Entries on the same host with different ports are different origins.
