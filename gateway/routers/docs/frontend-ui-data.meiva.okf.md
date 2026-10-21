---
type: doc
title: Frontend UI data routers
description: The route modules serving the frontend's read surfaces — fleet graph, timeline, notices, pages (+ streaming proxy), the Grafana reverse proxy, and plugin UI contributions/mounts.
tags: []
---

# Frontend UI data routers

- **fleet_graph** (`/api/fleet/graph`) — weighted agent relationship graph; nodes carry canonical lifecycle `status`, immutable birth parent (`born_spawner`), independent `liveness_state` from `agents_meta`, and token sums from `telemetry_events`; edges are aggregated in Postgres from `audit_events`
- **timeline** (`gateway/agents/history/timeline.py`, `/api/agents/{id}/timeline`) — current checkpoint tail plus optional scroll-up pages from retained `compact_boundary` checkpoints. Current cursors are `msg.block`; history cursors are `s<rank>.<checkpoint_id>.<msg>.<block>`, resolved by checkpoint id and bounded by `MEIVA_TIMELINE_COMPACT_HISTORY` (`0` off / positive newest-N / `-1` all). A segment-head response repeats its re-attached prompt/summary before the next older tail so a page boundary cannot hide the summary; a historical summary can continue across a re-attached-only boundary one rank per request. Crossovers may read source and target sequentially but keep at most one full segment resident. Bad, missing, non-boundary, or depth-ineligible historical ids terminate rather than selecting another segment ([decision](../../../docs/decisions/agents/context/2026-08-25-timeline-compact-history-pagination.md)).
- **notices** (`gateway/agents/notices.py`, `/api/notices/*`) — response-required questions with independent P0–P3 priority and blocking. `meiva.ui.notify()` posts a notice; ordinary information stays in chat. Open/live feeds and resolved history exclude legacy FYIs. The unified feed carries open questions in `awaiting`; its legacy `open` array remains empty. Users answer or explicitly dismiss via `POST /api/keyed/v1/agents/{id}/notices/{notice_id}/resolve`; fresh read operations are rejected. Agents withdraw with `meiva.ui.dismiss_notice()`. Answers are stored on the row and delivered as `system:notice-reply`; dismissal is `system:notice-dismiss`. Both are system-sourced inbounds. `task_id` links the notice to a task. Every new notice stages `core.needs_input` delivery in the same transaction (see [[base/events/notifications/docs/notifications.meiva.okf.md|the notification spine]]).
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

## Notice feed pagination

The open feed sorts by priority, blocking and oldest creation time; `agent_id`
narrows `/api/notices/open` before its limit. The live feed reads global IDs
strictly greater than `after`, ordered by increasing ID. Open/live `limit`
defaults to `settings.display.notices_open_default_limit`.

`GET /api/notices` returns open questions in `awaiting` and one `resolved_page`.
Resolved history is ordered by `(resolved_at, id)` descending. Pass both
`before_at` and `before_id` from `next_cursor` to read strictly older rows;
supplying only one is rejected. A null `next_cursor` ends pagination. The
`resolved_limit` default belongs to `settings.display.notices_resolved_default_page`.
