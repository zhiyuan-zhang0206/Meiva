# Gateway Routers

`gateway/app.py` mounts route modules. Cohesive route/helper/model domains live
in `gateway/<feature>/`; single-module routers live in
`gateway/routers/<domain>.py`, with models in `gateway/schemas/<domain>.py`.
`upload/` owns intake, `receipts/` owns replay, and `gateway/inspect/` owns the
OKF viewer. Fleet graph keeps its route-local query.


| package | routers |
|---|---|
| `gateway/agents/` | `router` (agents), `lifecycle`, `state`, `history.conversation`, `history.timeline`, `notices`; helpers `forward`, `delivery` |
| `gateway/events/` | `router` (events), `agent_events`, `computer_traces`, `resolutions`, `metrics`, `system` |
| `gateway/cluster/` | `router` (cluster), `machine_pause`, `bootstrap`, `status`, `ops_monitor` |
| `gateway/extensions/` | `inventory`, `skills`, `plugin_ui`, `ui_contributions`, `packages` |
| `gateway/routers/configuration/` | `runtime`, `default_model` |
| `gateway/alerts/`, `gateway/http/auth/`, `gateway/mcp_server/`, `gateway/schedules/`, `gateway/inspect/` | `router` |

## Router categories

### Agent core (lifecycle + observability)
- **agents** (`/api/agents/*`) — [lifecycle and projection contract](../../agents/docs/agents-router/agents-router.md).
- **agent_inbound** (`/api/agents/{id}/inbound`) — one agent's inbound queue as bounded history, newest first: every status and kind, filters status / kind / source / hours / limit, content a preview unless `full`; one agent's rows only
- **agent_events** (`/api/agents/{id}/events` + `/events/stream`) — historical REST query over the unified event stream (Loki) + real-time SSE tail (filtered by agent_id)
- **events** (`/api/events`) — unified event stream query (Wave 2): every category (audit / telemetry / log) through one surface: audit rows from Postgres `audit_events`, telemetry/log from Postgres `telemetry_events`, merged newest-first; filters category/event_name/agent_id/trace_id/machine/level + time window (`from`/`to` or `hours`) + offset paging, `meta` (total/window/has_more) envelope; `/api/events/counts` counts the same filtered stream per value of one dimension (`by` = event_name / machine / level / agent_id / category) in SQL, largest group first, under the same statement timeout
- **run_timeline** (`/api/agents/{id}/run-timeline`) — see [the understanding tree and message units over a window](../../../services/derived/insights/run_timeline/docs/run_timeline.md) (its `/messages` range read included).
- **inspect** (`/api/agents/{id}/inspect/*` + `/neighbors`) — per-agent LLM cost/token/TPS + neighbor graph; plugin metric and inspector-widget surfaces — [Agent Inspect Router](../../inspect/docs/inspect.md)
- **system** (`/api/system`, `/api/agents/{id}/system`, `/api/system/all`) — SSE broadcasting (see [SSE (Server-Sent Events)](../../events/docs/sse/sse.md))
- **delivery** — chat inbound delivery helper (not a router); gateway callers attach server-owned credential, transport, content-hash, and source-assertion facts at the durable insert
- **ops_monitor** (`/api/ops/monitor`) — time-bucketed ops panel series (SSE backlog / LLM latency+TPS / restart counts), see [Ops Monitor Router](../../cluster/docs/ops-monitor.md)
- **alerts** (`/api/alerts` + `/stream` + `/read`) — the system→human alert store (Alertmanager shape, `alerts` table), unresolved-first list + counts, SSE tail, mark-as-read [Alerts Router](../../alerts/docs/alerts.md)
- **event_resolutions** (`/api/event-resolutions`) — authenticated immutable-Loki warning/error class dismissal history: create, status-filtered review list, and manual reopen; writes `event_dismissals` and emits transition markers, while the events-maintenance daemon publishes the resulting gauges

### Cluster & configuration
- **cluster** (`/api/cluster/*`) — cluster status, multi-machine roster, admin events, and maintenance control (admin contracts: [Ops Surfaces (cluster admin + stats dashboard)](../../cluster/docs/ops-surfaces.md))
- **bootstrap** (`/api/bootstrap`) — agent-runner registration handshake (returns cluster config; `MEIVA_DB_URL` is the credential-free endpoint, never a login, and the human secret is never served; admits a unit's machine API token)
- **configuration** (`/api/config*`) — [runtime config and the default model](../configuration/README.md)
- **settings** (`/api/settings`) — frontend user preference KV store (`user_settings` table)
- **frontend_telemetry** (`POST /api/frontend-telemetry`) — user-modeling telemetry ingest: validates a batch of tracked frontend interactions (page/element/session_id/key/value, no free text) and emits one `frontend_interaction` event per accepted interaction into the unified stream (per-session rate-limit backstop)
- **inventory** (`/api/inventory`) — cross-machine plugin + MCP enable/disable panel
- **skills** (`/api/skills`) — read-only: this machine's `$MEIVA_HOME/skills/` load dir × install registry view (layer=core/plugin/machine/untracked, `modified_locally` drift flag). Unlike inventory, skills are **per-machine** (no cluster-shared rows)—no `?machine=` matrix, reports only this gateway's own
- **packages** (`POST /api/keyed/v1/packages/draft`) — **install entry** for skill/plugin/MCP: `{kind, nl}` → fixed prompt to `meiva.skills.meiva_guide.packages.install` → spawn an installer agent, return `agent_id`. **Deliberately no URL/spec fields**—users can't judge candidate quality; candidate-finding, confirmation, install, test-agent verification, evaluation all happen in that agent's conversation. Same shape as guide/schedules draft (no DB row, no new state)
- **guide** (`POST /api/keyed/v1/guide/draft`) — same-shaped ops entry: spawn an `meiva-guide` agent to handle natural-language ops requests

### Frontend UI data

[Frontend UI data routers](frontend-ui-data.md)

### Ops & system
- **status** + **alert_classes** (`/api/health`, `/api/status`, `/api/stats/dashboard`, `/api/stats/alert-classes[/samples]`) — liveness + status panel + dashboard event totals + warning/error class diagnostics; public health exposes process `started_at` and boot-frozen `sha` for rollout observers ([dashboard contract](../../cluster/docs/ops-surfaces.md), [alert classes](../../cluster/docs/alert-classes-api.md))
- **metrics** (`/api/metrics`, `/api/metrics/agents`) — aggregated metrics over `telemetry_events`
- **schedules** (`/api/schedules/*`) — scheduled task CRUD + start/stop/restart
- **shell** (`/api/agents/{id}/shell/{sid}`) — shell session monitor (session backend proxy)
- **tasks** (`/api/tasks` GET with optional `owner` / `status` filters and `window`, `/api/tasks/{id}` GET + PATCH) — [task registry read + keyed partial update](task-patch-receipts.md) (no create; an owner reassignment notifies the new and, when live, previous owner); the list defaults to a full compatibility row or serves a SQL projection — `fields=summary` (metadata only) or `fields=graph` (summary plus `description`, never `results`) — and the single-task GET is the hover card's read of one full row, including `results`; rows carry `priority` (`P0`..`P3`, validated, illegal 422)
- **memory** (`/api/memory/search`, `/refresh`, `/graph`) — [shared checkout ownership and bounded search](memory.md); a search stops when its client disconnects
- **commands** (`/api/commands`) — slash command list acceptable by composer
- **auth** (`/api/auth/login|logout|check|sessions`) — opaque server-side session login, validation, listing, and per-session revocation
- **uploads** (`/api/agents/{id}/uploads/{filename}`) — [delivered workspace file download](upload-batches.md); staged intake and batch delivery to the agent live on `/api/keyed/v1/agents/{id}/uploads*`.

## Design principles

Dependency direction, the no-turn-loop rule and its enumerated exception, the
handler/mounting split, and boundary typing:
[Gateway Router Design Principles](design-principles.md).

## Entry points

- `gateway/routers/__init__.py` — empty file, router modules are independent
- `gateway/app.py` — mounting point for all routers (`app.include_router(x.router)`), in a fixed order

## Notes

New endpoint → add it to its feature package, or create `gateway/routers/<domain>.py` for a new single-module surface; then `include_router` in `app.py`. Frontend/CLI/SDK share the same endpoints.
