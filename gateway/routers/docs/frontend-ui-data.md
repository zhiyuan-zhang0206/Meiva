# Frontend UI data routers

- **fleet_graph** (`/api/fleet/graph`) — weighted agent relationship graph; nodes carry canonical lifecycle `status`, immutable birth parent (`born_spawner`), independent `liveness_state` from `agents_meta`, and token sums from `telemetry_events`; edges are aggregated in Postgres from `audit_events`
- **timeline** (`gateway/agents/history/timeline.py`, `/api/agents/{id}/timeline`) — current checkpoint tail plus optional scroll-up pages from retained `compact_boundary` checkpoints. Current cursors are `msg.block`; history cursors are `s<rank>.<checkpoint_id>.<msg>.<block>`, resolved by checkpoint id and bounded by `MEIVA_TIMELINE_COMPACT_HISTORY` (`0` off / positive newest-N / `-1` all). A segment-head response repeats its re-attached prompt/summary before the next older tail so a page boundary cannot hide the summary; a historical summary can continue across a re-attached-only boundary one rank per request. Crossovers may read source and target sequentially but keep at most one full segment resident. Bad, missing, non-boundary, or depth-ineligible historical ids terminate rather than selecting another segment ([decision](../../../docs/decisions/agents/context/2026-08-25-timeline-compact-history-pagination.md)).
- **questions** (`gateway/agents/questions/resolve.py`, `/api/questions*`) — the unresolved questions. `meiva.ui.ask()` posts a question (a question has no priority, expiry or blocking flag); ordinary information is delivered with `meiva.ui.publish` or a displayed reply. `GET /api/questions` returns the fleet's unresolved questions in `awaiting`, and `GET /api/questions/open` the same list with an optional `agent_id`; there is no resolved-history page, no live feed and no FYI list. Users answer or explicitly dismiss via `POST /api/keyed/v1/agents/{id}/questions/{question_id}/resolve`; fresh read operations are rejected. Answers are stored on the row and delivered as `system:question-reply`; dismissal is `system:question-dismiss`. Both are system-sourced inbounds.
- **grafana** (`/grafana/*`, outside `/api`) — optional streaming reverse proxy to a co-located Grafana instance (`MEIVA_GRAFANA_PROXY_ENABLED`, `MEIVA_GRAFANA_HOST`/`MEIVA_GRAFANA_PORT`, default off → 404), auth-gated by the same cluster middleware, for dashboard iframes
- **ui_contributions** (`gateway/extensions/ui_contributions.py`, `/api/ui/contributions`) — the merged, plugin-attributed `contributions.ui` declaration set of the cluster's ENABLED plugins (theme token packs, nav entries, and status-panel cards today; agent-inspect sections as that slice lands). Read straight from each plugin's `meiva-plugin.json` — no plugin code is imported to answer it [Package Manifest (spec v2)](../../../docs/plugins/package-manifest.md)
- **plugin_ui** (`gateway/extensions/plugin_ui.py`, `/api/plugin-ui/<plugin>/…`) — static files from an ENABLED plugin's own `ui/` directory, for the sandboxed iframe the console embeds. segment validation plus a resolved-path containment check

Reverse-proxy contracts live in `gateway/tests/routers/proxies/`: Grafana
preserves authenticated forwarding, streaming, error responses
and path-traversal rejection.

Traversal tests encode the path so the HTTP client cannot normalize literal
`..` before it reaches the server. Plugin UI requires 400 for encoded dot
segments; upload tests use `..%2f..%2fsecret` and accept 400 or 404. That upload
assertion alone does not establish that a particular resolver ran.

## Question feed

Both feeds sort by question id; `agent_id` narrows `/api/questions/open`
before its limit. Fleet-wide, `limit` defaults to `settings.display.questions_open_default_limit`; for one
agent the whole open set is returned unless `limit` is given, because an answer must name exactly that set
(the 409 of the answer route carries it whole too).
Resolved questions are not listed: a resolution removes the row from both feeds
and the frontend learns of it through `question_resolved`.
