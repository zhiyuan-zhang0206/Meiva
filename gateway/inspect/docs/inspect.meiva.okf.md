---
type: doc
title: Agent Inspect Router
description: "GET /api/agents/{id}/inspect/statistics + /inspect/live + /neighbors + /inspect/metrics + /inspect/widgets — per-agent LLM cost/token/TPS and neighbor graph, plus the plugin metric and inspector-widget surfaces."
tags:
- gateway
- observability
---

# Agent Inspect Router

`/api/agents/{id}/inspect/statistics` + `/inspect/live` + `/neighbors` + `/inspect/metrics`
+ `/inspect/widgets` — per-agent LLM cost/token/TPS + neighbor graph.

The live inspector's heartbeat block — the four display states (a pause, a queued wake,
a projected check-in, nothing) and the daemon predicate each one mirrors — has its own
node: [[gateway/inspect/docs/heartbeat.meiva.okf.md|Inspector heartbeat projection]].

Statistics read persisted compact metric observations and day summaries from
Postgres. Cache-hit rates divide reported cache reads by `tokens_cache_input`,
the input from calls with cache metering. Unmetered decision and embedding
calls remain in usage and cost totals, but not in the cache-rate denominator.
Nonzero input without metering yields a null rate (the UI renders N/A).
A null stored denominator identifies a legacy observation/day and preserves
its historical input denominator; new unmetered calls contribute zero. Pre-cutover full days preserve the existing cost/turn ledgers without
adding the same observed day twice. Exact duration observations feed quantiles;
older ledger-only histograms retain their declared integer-second precision.
The panel renders the observed metrics only: coverage verdicts (availability,
duration precision, retained sources, last-observed) go to the background log
instead, with unexpected gaps emitted as the `inspect_metrics_coverage_gap` event
(task #3869, user ruling 2026-09-17). Which limits a read shows, and which is unexpected, is the strict
`gateway/inspect/_coverage.py` (`coverage_notes`); `_metrics_health.py` only logs them.
Missing evidence is null/partial with window and observation timestamps, not a
zero or a claim of complete collection. No synchronous log scan or completed
result TTL is part of the statistics path. The statistics path serves only the
explicit hour windows whitelisted by `StatsWindowHours` (0/1/6/24/72/168/720); it
has no since-compact window (a window over compact boundaries would first need
an authoritative completed-compact stamp — a separate execution-contract
decision). The `/inspect/live` half remains
window-independent — see [[gateway/cluster/docs/ops-surfaces.meiva.okf.md]].

The Gateway lifespan owns shared SQL work until its native executor future
finishes, including after an HTTP waiter times out or disconnects. Identical
in-flight requests share one load, with at most four distinct-key leaders;
followers occupy no worker thread. Closing stops admission and drains these
physical calls before the pool closes. Each completed load retires its in-flight
claim and releases admission before notifying followers, including on failure;
a subsequent zero-TTL request therefore starts a fresh load. Loader errors still
reach active callers;
late failures go to the event loop's error reporting boundary with the query key,
without retries, replacement snapshots, or cancellation of unrelated service work.

Shell deadlines in `/inspect/live` and the shell monitor come only from
`agent_shell_ttls`. Row-less page and schedule sessions render no shell TTL;
their lifecycle is managed separately. Launch time never synthesizes a deadline.

## Plugin metric surface (`/inspect/metrics`, W13b)

Builds the metric registry in process — ENABLED shipped plugins' `metrics.py` declarations
(`contribute()`, admitted into a data registry) + core definitions — renders `output`-inspector templates per agent, re-validates
the rendered query, evaluates LogQL templates on `telemetry_events` / `audit_events` (`gateway/inspect/_event_metrics.py`) and SQL read-only over Postgres —
see `base/telemetry/metrics/plugin_metrics.py` + the `deploy/lgtm` dashboards README.
Which plugins are enabled is read once per request (`_plugin_faces.enabled_plugin_names`, from the machine's plugin config) and
handed to the loader as a set of names; the loader and the per-agent execution (`metrics_for_agent(pool, agent_id, registry=..., clock=...)`) take values.

## Plugin inspector-widget surface (`/inspect/widgets`, task #2909)

Builds the widget registry in process — ENABLED builtin plugins' `inspector.py`
declarations (`contribute()`, admitted into a data registry) — and resolves each widget's payload
server-side: a `taskList` lists the agent's active tasks (owner-scoped,
priority-ordered (P0 first, ties by id), complete; each row carries the
task's id, title, and its P0..P3 priority — tasks #3819/#3866). A widget with
nothing to show leaves the payload
entirely — see `base/packages/plugins/surface/inspector.py`. An `inspector.py` that
fails to import is skipped with a loud report (loguru ERROR + the
`plugin_load_failed` event) and the remaining widgets still serve — fail-soft
per the plugin-load contract (user ruling 2026-09-11); a declaration is admitted
whole or not at all, so the next request retries clean.

## Repository graph

`okf_graph.py` serves `GET /api/okf/graph` from the current documentation tree,
using the shared `base.packages.docs.okf_graph` renderer. The HTML view uses
the gateway's normal request authentication; its route and rebuild behavior
are unchanged.
