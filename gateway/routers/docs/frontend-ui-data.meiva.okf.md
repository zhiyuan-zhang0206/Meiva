---
type: doc
title: Frontend UI data routers
description: The route modules serving the frontend's read surfaces — fleet graph, timeline, notices, pages (+ streaming proxy), the Grafana reverse proxy, and plugin UI contributions/mounts.
tags: []
---

# Frontend UI data routers

- **fleet_graph** (`/api/fleet/graph`) — weighted agent relationship graph; nodes carry canonical lifecycle `status`, immutable birth parent (`born_spawner`), independent `liveness_state` from `agents_meta`, and token sums from `telemetry_events`; edges are aggregated in Postgres from `audit_events`
- **timeline** (`gateway/agents/history/timeline.py`, `/api/agents/{id}/timeline`) — current checkpoint tail plus optional scroll-up pages from retained `compact_boundary` checkpoints. Current cursors are `msg.block`; history cursors are `s<rank>.<checkpoint_id>.<msg>.<block>`, resolved by checkpoint id and bounded by `MEIVA_TIMELINE_COMPACT_HISTORY` (`0` off / positive newest-N / `-1` all). A segment-head response repeats its re-attached prompt/summary before the next older tail so a page boundary cannot hide the summary; a historical summary can continue across a re-attached-only boundary one rank per request. Crossovers may read source and target sequentially but keep at most one full segment resident. Bad, missing, non-boundary, or depth-ineligible historical ids terminate rather than selecting another segment ([decision](../../../docs/decisions/agents/context/2026-08-25-timeline-compact-history-pagination.md)).
- **notices** (`gateway/agents/notices.py`, `/api/notices/*`) — the agent→user notice surface. An agent calls `meiva.ui.notify()` to post one at a rung of `require_response` x `blocking`; a notice that needs a response rides the agent snapshot inline (a bounded worklist), while an FYI notice stays off it (the snapshot carries only `unread_notice_count`) and `GET /api/notices/open` is the FYI feed's source. Both kinds surface in the resolved page of `GET /api/notices` once resolved. The user resolves a notice (answer / dismiss / read) through `POST /api/keyed/v1/agents/{id}/notices/{notice_id}/resolve`, and the agent withdraws it with `meiva.ui.dismiss_notice()`. A reply is cached on the row's `reply` column and delivered to the agent as a `system:notice-reply` inbound — the column is a cache, not the delivery channel — and a bare dismiss is `system:notice-dismiss`; both are system-sourced, so no user-role message is consumed and nothing asks for a reply. Rows carry `task_id` (`null` when unattached), driving notice→task linkage in the inspector. A notice that needs an answer stages its `core.needs_input` delivery in the same transaction (see [[base/events/notifications/docs/notifications.meiva.okf.md|the notification spine]]).
- **pages** (`/api/pages`, `/api/agents/{id}/pages`, keyed register/close under `/api/keyed/v1/agents/{id}/pages`) — `meiva.ui.show/close` registered UI pages + the streaming reverse proxy for them (`/pages/{id}-{name}/...` → the page server; browser never dials it directly)
- **grafana** (`/grafana/*`, outside `/api`) — optional streaming reverse proxy to a co-located Grafana instance (`MEIVA_GRAFANA_PROXY_ENABLED`, `MEIVA_GRAFANA_HOST`/`MEIVA_GRAFANA_PORT`, default off → 404), auth-gated by the same cluster middleware, for dashboard iframes
- **ui_contributions** (`gateway/extensions/ui_contributions.py`, `/api/ui/contributions`) — the merged, plugin-attributed `contributions.ui` declaration set of the cluster's ENABLED plugins (theme token packs, nav entries, and statistics-panel cards today; agent-inspect sections as that slice lands). Read straight from each plugin's `meiva-plugin.json` — no plugin code is imported to answer it [[docs/plugins/package-manifest.meiva.okf.md]]
- **plugin_ui** (`gateway/extensions/plugin_ui.py`, `/api/plugin-ui/<plugin>/…`) — the sibling mount of `pages`: static files from an ENABLED plugin's own `ui/` directory, for the sandboxed iframe the console embeds. `pages`' segment validation plus a resolved-path containment check

Reverse-proxy contracts live in `gateway/tests/routers/proxies/`: Grafana and
agent page servers preserve authenticated forwarding, streaming, error responses
and path-traversal rejection.

Traversal tests encode the path so the HTTP client cannot normalize literal
`..` before it reaches the server. Plugin UI requires 400 for encoded dot
segments; upload tests use `..%2f..%2fsecret` and accept 400 or 404. That upload
assertion alone does not establish that a particular resolver ran.
