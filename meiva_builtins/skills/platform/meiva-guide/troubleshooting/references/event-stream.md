# Read the event stream — LogQL over Loki

Loki holds the unified event river: every agent, every service, one stream
family. This is where a run investigation starts, because it is the only
surface that covers an unbounded agent life rather than one bounded trace.

Reach it three ways: `meiva events` (Postgres subset, no LogQL), the gateway's Grafana
datasource proxy with the `gq` helper in [SKILL.md](../SKILL.md#query-the-stack-from-any-machine)
(any machine), or Grafana Explore.

## The dialect

`agent_id`, `machine`, `event_name` and `service_name` are Loki index labels, so the
selector itself narrows the stream:

```logql
{agent_id="3048"}
{agent_id="3048", machine="company-mini"}
{machine="macmini", event_name="exec_failed"}
```

Every event is shipped as one stream family with `service_name="unknown_service"`; the
selector needs no `service_name` when it names another label. Agent selectors cover
explicitly attributed events and agent-owned exec process logs. Ordinary unannotated
shared-host logs use process/system attribution and are absent from an `agent_id`
selector; inspect them by `machine`, process, time or trace.

Rules the whole tree obeys:

- **Index labels go in the selector; the rest is structured metadata.** `level`, `category`,
  `process`, `trace_id` are not labels: filter after the selector with
  `| level="error"` or `| trace_id="<hex>"` (no `| json` needed for these).
  Payload fields need `| json` first (`attributes_cost_usd`, below).
- **`machine` became an index label on 2026-11-24** (`v2026.11.24.001256`). Older samples carry it
  only as metadata, so a window across that date uses `{...} | machine="x"` for the old part.
- **Wrap every count in `sum(...)`.** An unaggregated `count_over_time` over many streams hits
  Loki's per-query series cap.

Retention is 7 days (168h, `deploy/lgtm/native/config/loki.yaml`) and Loki never holds DEBUG. The
per-machine JSONL event mirror (`$MEIVA_HOME/logs/events-*.jsonl`, also 7 days) holds every event
Loki missed; DEBUG is only in the machine's local files.

## The line shape

Each line's body is the whole event as JSON:

```
ts, trace_id, span_id, agent_id, machine, process, category, event_name,
level, source, target_agent_id, attributes
```

`| json` flattens the nested payload with underscores: `attributes.cost_usd`
becomes `attributes_cost_usd`, and numbers parse as numbers, so
`| attributes_age_s < 600` is a real numeric comparison.

`category` is `audit | telemetry | log`. `level` is
`debug | info | warning | error | critical`, lowercase. The `event_name`
vocabulary is the registry in `base/events/contract.py`, declared per domain in `base/events/declarations/` — read it there
rather than guessing a name.

**Multiple extractions in one `| json` stage is a parse error.** Each field
needs its own stage: `| json model="attributes.model" | json cost_usd="attributes.cost_usd"`.

## Narrowing to one run

```logql
{agent_id="3048"}                                  # one agent
{agent_id="3048"} | trace_id="<lowercase-32-hex>"  # one traced stretch's whole call chain
```

Lowercase the trace id before matching. Service-level (non-agent) rows have no `agent_id`
label: select them by `machine` and `process`.

The gateway reads Postgres for `GET /api/events` (what `meiva events ls` calls):

```
GET /api/events?agent_id=3048&hours=6
GET /api/events?trace_id=<hex>&from=<ISO-with-offset>
```

Postgres keeps only the events a reader queries by name (`persist=True` in the registry) and
every event at warning level or above; the rest of a trace's chain (`exec_envelope`,
`hook_timing`, `node_exit`, info-level logs) is not there. The whole chain is in Loki within its
7-day window (`| trace_id="<hex>"`) and in the per-machine JSONL event mirror.

`from` must carry a timezone offset. Absent both `from` and `hours` the window is forced to the
last 24h. `limit` caps at 1000; paging is `offset`, an exact count is opt-in via `with_total=1`.

## What went wrong — the recipes

**Exec outcomes.** The outcome is the event name, not an attribute:
`exec` is success; failures are `exec_failed`, `exec_timeout`,
`exec_cancelled`, `exec_node_timeout`.

```logql
sum(count_over_time({agent_id="3048", event_name=~"exec_.+"} [1h]))
```

The failing code itself rides on `attributes_body`; `exec_failed` also carries
`attributes_exc_type`.

**LLM usage and spend.** `event_name="llm_usage"` carries `attributes_model`,
`attributes_calls`, `attributes_in_total`, `attributes_out_total`,
`attributes_cache_read`, `attributes_reasoning`, `attributes_latency_ms`,
`attributes_cost_usd`.

```logql
sum(sum_over_time({agent_id="3048", event_name="llm_usage"} | json | unwrap attributes_cost_usd [$__range]))
```

**Turn outcomes.** `event_name="turn_end"` carries `attributes_ok` (the string
`"true"`/`"false"`) and `attributes_duration_seconds`.

```logql
sum(count_over_time({agent_id="3048", event_name="turn_end"} | json | attributes_ok="false" [24h]))
```

**Halt reasons.** `event_name="halt"`, reason in `attributes_body`. The four
observed classes: `"no tool_call (idle)"`, `"system_halt (compact)"`,
`"lifecycle AgentTermination"`, `"lifecycle AgentRestart"`. Match a class with
`| attributes_body=~"lifecycle .*"`.

**Everything that raised a flag.**

```logql
{agent_id="3048"} | level=~"warning|error|critical"
```

**Resurrections.** `event_name="resurrect"`, `category="audit"` — one row per
resurrection: a chat message waking a terminated agent, or an operator's explicit
resurrect. A crash does not resurrect an agent by itself, so a run of these rows
is not a crash loop.

## Quantiles lie a little

Loki's `quantile_over_time` is per-series, and each event is its own series.
The core dashboards aggregate per-stream quantiles with `max()`, which tilts a
p50 toward the slowest agent process. `max` itself is exact; treat a p50 read
off a dashboard as an upper-ish estimate, not a median.

## Raw session logs are a different namespace

Raw stdout shipped by the collector's filelog receivers has no JSON structure.
The receiver split is intentional: `filelog/sessions` admits only agent shell
transcripts, while `filelog/services` admits gateway/daemon/schedule output and
excludes every `meiva-agent-*` main log. Both set resource `service.name` from the
file name, which Loki exposes as `service_name`:

```logql
{service_name="meiva-agent-1818-shell-1"}
{service_name=~"meiva-agent-.+-shell-.+"} |~ "(?i)traceback"
{service_name="meiva-gateway"} |= "error"
```

The banner-only agent main stdout is not ingested; its structured records
already arrive through OTLP. Go to the shell or service stream for a stack
trace the event stream only summarizes.

## Where things listen

| Component | Reach it |
|---|---|
| Loki, Prometheus, Tempo | the station's loopback only; from any machine through the gateway: `<gateway>/grafana/api/datasources/proxy/uid/<loki\|prometheus\|tempo>/...` ([`gq`](../SKILL.md#query-the-stack-from-any-machine)) |
| Grafana | `<gateway>/grafana/` (anonymous viewer behind the gateway's auth; datasource uids `loki`, `prometheus`, `tempo`) |
| Gateway events API | `<gateway>/api/events` (what `meiva events` calls; bearer is the machine token) |

`meiva observability status` reports the marker, containers, and readiness probes; the
whole stack only exists on the host holding the `$MEIVA_HOME/lgtm-host` marker.
