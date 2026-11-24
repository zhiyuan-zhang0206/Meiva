---
name: troubleshooting
description: "Finds out what happened to an agent, machine or service around a given time. Use for any bug, stuck or misbehaving agent, alert, or 'what happened at T' question, before reading log files by hand."
---

# Troubleshooting: what happened to X around time T

Start in the observability stack, not in `~/.meiva/logs`. Every machine's events,
logs, spans and metrics already land in one place, joined by two ids, so one query
from any machine answers what grepping five hosts over ssh would.

| Id | Where it appears |
|---|---|
| `agent_id` | `meiva events ls --agent`, Loki label `agent_id`, Tempo `span.meiva.agent_id` (int) and `resource.meiva.agent_id` (string) |
| `trace_id` (32 hex) | the `trace_id` field of every event and log line, `meiva events ls --trace`, Tempo trace id |

Also join on `machine` (Loki index label, `meiva events ls --machine`, Tempo `resource.meiva.machine`).

## The path

Take the steps in order and stop when you have the cause.

1. **Read the alert first.** An alert message carries `Events: meiva events ls ...`
   (the exact filter that fired) and a Grafana link. Run the command as written.
2. **Structured events.** `meiva events ls --agent <id> --hours 6` (add `--machine`,
   `--level warning`, `--name`, `--from/--to` with an offset; `meiva events count --by event_name`
   lists what occurred). Runs on every machine, no ssh. Telemetry and log events are Loki's 7
   days, audit events Postgres' permanent record.
3. **Loki window query**, for what `meiva events` does not do (LogQL aggregates, `| json` fields). See
   [Query the stack from any machine](#query-the-stack-from-any-machine).
4. **A `trace_id` opens the whole causal chain.** `meiva events ls --trace <id>` and
   `{machine="<m>"} | trace_id="<id>"` in Loki give the events; Tempo gives the span tree
   (durations, node path, LLM calls, cross-service hops). See [Traces](#traces).
5. **Metrics for "is it slow, saturated or rising"**: Grafana dashboard `meiva-ops-main`, or PromQL
   below. Never use Prometheus `meiva_llm_usage_*_total` for cost (`increase()` loses
   each new process's first sample); cost is `meiva stats` / `meiva metrics agents`.
6. **Last: the DEBUG files on the machine itself** ([Local files](#local-files-last)). Only
   DEBUG lives there alone; reach for it once 1-5 say where to look.

## Wrong store means a false "it did not happen"

Zero rows from one store does not mean the thing did not happen. Before concluding
"never ran / nothing lost":

- `meiva events` reads Loki for telemetry and log events and Postgres for audit events, so it
  sees what Loki sees, never DEBUG. Lifecycle request/result events are audit events written to
  Postgres by the CLI and are almost absent from Loki, so `--category audit` is where they are.
  Check the declaration in `base/events/declarations/` to know the category.
- Loki keeps 7 days (168h, `deploy/lgtm/native/config/loki.yaml`) and never DEBUG. Older: audit
  events in Postgres, then the per-machine `$MEIVA_HOME/logs/events-*.jsonl` (also 7 days).
- Always narrow by `machine`: one agent id's query without it can read as another host.
- Tempo spans carry no content; messages come from the checkpoint ([find-the-run](references/find-the-run.md)).
- `harness_exit_code` is not a failure signal (antigravity logs 1 on a normal terminate).

## Query the stack from any machine

Loki, Prometheus, Tempo and Grafana listen on the observability station's loopback. Every
other machine reaches them through the gateway's `/grafana` proxy and Grafana's datasource
proxy, read-only, with the machine token a Meiva process already holds. Importing the `meiva` module
puts `MEIVA_API_TOKEN` into the Python environment (a plain shell may not have it), so
the helper below goes through `$MEIVA_PYTHON`. Define it once per shell:

```bash
gq() { "$MEIVA_PYTHON" -I - "$@" <<'PY'
import os, sys, urllib.parse, urllib.request
import meiva  # the import delivers MEIVA_API_TOKEN into os.environ
path, *kv = sys.argv[1:]
url = os.environ["MEIVA_GATEWAY_URL"].rstrip("/") + "/grafana/api/datasources/proxy/uid/" + path
url += "?" + urllib.parse.urlencode([tuple(a.split("=", 1)) for a in kv])
req = urllib.request.Request(url, headers={"Authorization": "Bearer " + os.environ["MEIVA_API_TOKEN"]})
print(urllib.request.urlopen(req, timeout=60).read().decode())
PY
}
```

`gq <datasource-uid>/<upstream path> key=value ...` prints the upstream JSON (`jq` filters it).
Datasource uids: `loki`, `prometheus`, `tempo`. Do not print the token or paste it into a message.

```bash
# Loki: one agent on one machine, last 30 minutes, oldest first, message bodies only
gq loki/loki/api/v1/query_range 'query={agent_id="2697", machine="company-mini"}' \
  since=30m limit=200 direction=forward | jq -r '.data.result[].values[][1]'

# Loki: warnings and errors on a machine (level and trace_id are structured metadata: filter after the selector)
gq loki/loki/api/v1/query_range 'query={machine="macmini"} | level=~"warning|error|critical"' since=1h limit=100

# Loki: a fixed window (ns epoch) and a payload field (`| json` flattens attributes_*)
gq loki/loki/api/v1/query_range 'query={agent_id="2697", event_name="llm_usage"} | json | attributes_calls > 0' \
  start=$(date -d '2026-11-24 13:00 +0800' +%s)000000000 end=$(date -d '2026-11-24 14:00 +0800' +%s)000000000

# Prometheus: instant query (host series carry the roster machine name)
gq prometheus/api/v1/query 'query=system_cpu_load_average_5m{job="meiva-infra"}'
```

`{agent_id=..}`, `{machine=..}`, `{event_name=..}` and `{service_name=..}` are the index labels:
put them in the selector. Everything else (`level`, `trace_id`, `process`, `category`) is
structured metadata, filtered after it. Wrap every `count_over_time` in `sum(...)`. The
LogQL dialect, recipes (exec outcomes, spend, halts) and raw shell/service logs are in
[event-stream](references/event-stream.md). On macOS `date -d` is `date -j -f`; compute the ns timestamps
with `"$MEIVA_PYTHON"` there.

If `gq` returns 404, the gateway's Grafana proxy is off (`MEIVA_GRAFANA_PROXY_ENABLED=false`);
a plain `curl` without a token gets 401 (use `gq`, which runs through `$MEIVA_PYTHON`); inside `gq`, 401 means the token is invalid or expired and a `KeyError` on `MEIVA_API_TOKEN` means the process holds none.

## Traces

Tempo keeps traces 7 days and is lossy: a span the collector could not deliver is recorded nowhere, so a missing span is not proof the work did not run (the events are). An agent turn is one trace: the root span `meiva-agent-<id>`, with gateway, agent-ops and exec
spans in the same trace, and span links to the turns that supplied its input. Get a `trace_id`
from any event line, then:

```bash
gq tempo/api/traces/<trace_id> | jq '[.batches[].scopeSpans[].spans[]] | length'   # whole trace (5 MB cap)
S=$(date -d '-1 hour' +%s); E=$(date +%s)
gq tempo/api/search 'q={ span.meiva.agent_id = 2697 }' start=$S end=$E limit=10 \
  | jq -c '.traces[] | {traceID, rootTraceName, durationMs}'
```

Claude Code and Codex agents emit native spans too, tagged `resource.meiva.agent_id` (string) and
`resource.meiva.machine`:

| Query | Shows |
|---|---|
| `{ resource.service.name = "claude-code" && resource.meiva.agent_id = "2697" }` | `claude_code.interaction`, `.llm_request`, `.tool`, `.tool.blocked_on_user`, `.tool.execution` |
| `{ resource.service.name = "codex-app-server" && resource.meiva.agent_id = "2697" }` | `harness.operation` (with `thread.id`) |
| `{ resource.meiva.machine = "company-mini" && span.meiva.agent_id = 2697 }` | the Meiva-side turn spans for that agent on that machine |

TraceQL, the whole-trace fetch, the toolchain scripts and Tempo pitfalls: [spans](references/spans.md).

**Give the human a link, not a dump.** Grafana opens at `<gateway>/grafana/` (the address the user
opens Meiva at; `$MEIVA_GATEWAY_URL` is the tailnet address). Trace to logs and logs to trace are
linked in Grafana Explore. Build the Explore URL as in `render_trace.py` (inspect-a-trace scripts)
(`MEIVA_GRAFANA_URL=$MEIVA_GATEWAY_URL/grafana` points it at the proxy). Dashboards:
`meiva-ops-main`.

## Local files (last)

Use them when Loki and events lack the line: DEBUG, a daemon that died before it could ship, or
a stack trace the events only summarize. Go to the machine that owns the agent (`meiva agents get <id>`).

- `$MEIVA_HOME/logs/<service>.log` is the current file; Loguru rotations are
  `<service>.YYYY-MM-DD_HH-MM-SS_<pid>.log`, named for when the file **started** (a file named
  `2026-11-23_08-00-04` holds 11-23 08:00 to 11-24 08:00 local time).
- Lines carry the **machine's local time** with an offset (`+08:00`); events, Loki and Postgres are
  **UTC**. Convert before comparing windows, and widen a window across a rotation boundary.
- Exec subprocess output: `$MEIVA_HOME/logs/agent-<id>.log`. Services under a root:
  `$MEIVA_HOME/run/meiva-root/logs/<unit>/`.
- Raw shell and service stdout is also in Loki (`{service_name="meiva-gateway"}`, `meiva-agent-<id>-shell-<n>`).

## Deeper

| Need | Read |
|---|---|
| LogQL dialect, per-event recipes, raw session logs | [event-stream](references/event-stream.md) |
| TraceQL, whole-trace fetch, the checkout scripts `fetch_trace.py` / `read_trace.py` / `render_trace.py` | [spans](references/spans.md) |
| What was actually said: checkpoints, conversation content, psql | [find-the-run](references/find-the-run.md) |
| Alerts, process, disk, connectivity, schedules, delivery | [operations](../operations/SKILL.md) |
| Cost and cluster tables | [cluster-data](../cluster-data/SKILL.md) |

Not for: fleet trends (use the dashboards and alert rules), deploying LGTM (`deploy/lgtm/README.md`,
`meiva observability`), computer-use traces (`/api/computer/traces`).
