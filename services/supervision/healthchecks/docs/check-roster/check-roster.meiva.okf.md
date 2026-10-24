---
type: doc
title: Health probe module roster
description: Protocol helpers registered by service specs and root diagnostics.
tags:
- services
- healthchecks
---

# Health probe module roster

Application service probes report the configured endpoint protocol. Helpers in
this table do not own recovery. Readiness and diagnostic
registration are distinct; the root manifest determines which services run.

A standard `/healthz` daemon has no module here: `services/supervision/roster/healthz.py` derives
its identity probe from the service name, so a new daemon of that shape adds no
file to this directory and no row to this table.

<!-- lint:healthcheck-roster-table -->

| Module | Role | Protocol evidence |
|---|---|---|
| `browser.py` | Service protocol probe | Browser CDP response and profile facts |
| `browser_mcp.py` | Service protocol probe | Browser MCP protocol ping |
| `browser_reach.py` | Read-only diagnostic helper | Temporary browser fetch contrasted with host request |
| `computer_mcp.py` | Service protocol probe | Computer MCP protocol ping |
| `frontend.py` | Service protocol probe | Application HTTP response behind the entry gate |
| `gate.py` | Service protocol probe | Gate entry HTTP health, independent of app availability |
| `gateway.py` | Service protocol probe | Gateway serving and database query health |
| `lgtm.py` | Read-only diagnostic helper | Native backend protocol helpers and Loki write/read round trip |
| `mcp_daemon.py` | Service protocol probe | Shared MCP protocol ping |
| `insights.py` | Service protocol probe | `GET /healthz` over the Unix socket: this service, this home, the recorded pid |
| `memory_search.py` | Service protocol probe | Real exact-search POST |
| `otel_collector.py` | Service protocol probe | Valid OTLP request accepted |
| `redis_bridge.py` | Service protocol probe | RESP command through the relay gets a Redis reply |
| `protocol_probe.py` | Read-only diagnostic helper | Protocol result adapter and bounded Unix ping |
| `permissions_helper.py` | Read-only diagnostic helper | Parent helper ping and launchd failure classification |
| `prod_venv.py` | Read-only diagnostic helper | Bounded dependency check and isolated import smoke |
| `redis_acl.py` | Read-only diagnostic helper | Runtime-credential Redis PING |

`protocol_probe.py` observes application responses without a process/socket census.
Destructive lifecycle ownership remains separate from these read-only probes.

The roster binds `memory_search_probe` to required URI and embedding-provider
strings when `build_services()` runs, alongside its service ports and URLs.
Each call still sends a zero vector of the provider's dimension to `POST /search`
with a three-second timeout; it does not construct configuration or reload the
unit's environment. HTTP/JSON decoding failures and response errors that raise
`TypeError` report down; an unknown provider remains a configuration error before
network I/O. The existing verdict checks for `hits` membership without validating
the JSON container type; tightening that response contract is separate work.

The [[services/supervision/meiva_root_glue/docs/diagnostics.meiva.okf.md|diagnostic roster]] documents
platform/capability gates, per-check budgets, authenticated data-plane protocols, and
reporting. [[services/supervision/healthchecks/docs/check-roster/roster-notes.meiva.okf.md|Roster ownership]]
describes the documentation guard.
