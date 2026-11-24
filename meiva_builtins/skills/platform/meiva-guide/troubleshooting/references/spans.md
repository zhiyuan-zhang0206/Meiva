# Read the spans — Tempo and the toolchain

Spans give you what the event stream cannot — the tree, the durations, the
langgraph node path, and the shape of each LLM call. Come here once you already
have a `trace_id`.

**Check the trace's scope first.** The root span (`base/telemetry/tracing.py:turn_span`)
wraps one graph invocation in `services/agent_runner/agent_host/host.py`. The host admits
work before invoking the graph, so an idle agent has no parked graph invocation.
The claim node returns at idle; waiting for a Redis wake happens in the host
scheduler outside the turn span.

Historical traces can have different boundaries. The retired process runner
could include an inbound idle wait, sometimes recorded as a `claim idle-wait`
child span. Runs before 2026-08-20 could have a session-scoped root spanning
many turns. Read their span boundaries before interpreting wall-clock duration.

Two sources, in the order you should reach for them:

| Source | Where | Good for |
|---|---|---|
| **Tempo** | from any machine: `gq tempo/api/...` through the gateway ([SKILL.md](../SKILL.md#query-the-stack-from-any-machine)); on the station: `MEIVA_TELEMETRY_TEMPO_QUERY_URL` (the scripts below read it), else `http://localhost:3200` | finding traces (indexed, cross-agent) and fetching one |
| **Grafana** | `<gateway>/grafana/`, datasource uid `tempo` | letting a human browse |

Traces are telemetry: Tempo keeps them for 7 days and there is no other copy. A span
the collector could not deliver (its queue is bounded and in memory) is lost, and a
trace older than the retention is gone; the checkpoint and the event river
([find-the-run](find-the-run.md), [event-stream](event-stream.md)) are what outlive it.

Spans are **metadata-only**. Prompts, completions, tool arguments and results
are stripped at record time (`base/telemetry/tracing.py`) — nothing you do to Tempo will
produce them. Content comes from the checkpoint
([find-the-run](find-the-run.md)).

**Tempo's address is per-cluster config** (`MEIVA_TELEMETRY_TEMPO_ENDPOINT` for intake,
`MEIVA_TELEMETRY_TEMPO_QUERY_URL` for queries; see `deploy/lgtm/README.md`) and is a loopback
URL on the station. The scripts inherit `MEIVA_TELEMETRY_TEMPO_QUERY_URL`
(`MEIVA_TRACE_TEMPO_URL` overrides); anywhere else use `gq`, which needs no URL.

## Find: TraceQL

Verified against Tempo 3.0.2; the deployed version is 3.1.1 (`deploy/lgtm/native/versions.yml`).

| Want | Query |
|---|---|
| All traces | `{ resource.service.name = "meiva" }` |
| One trace | `{ trace:id = "<hex-32>" }` |
| Spans of one agent | `{ span.meiva.agent_id = 3048 }` (int) or `{ span.traceloop.association.properties.agent_id = "3048" }` |
| Spans on one machine | `{ resource.meiva.machine = "company-mini" }` |
| Claude Code / Codex native spans of one agent | `{ resource.service.name = "claude-code" && resource.meiva.agent_id = "3048" }` (`codex-app-server` for Codex; resource attribute is a string) |
| All of one agent's turn roots | `{ span.session.id = "3048" }` |
| One specific turn | `{ span.session.id = "3048" && span.meiva.turn = 17 }` |
| Spans in one langgraph node | `{ span.traceloop.association.properties.langgraph_node = "after_exec" }` |
| Spans by operation | `{ span.gen_ai.operation.name = "goto" }` |
| Exact span name | `{ name = "goto exec" }` |

Search parameters: `limit` (traces), `spss` (spans per span set, max 100),
`start`/`end` (unix seconds). Dotted attribute names work inside `span.*`.

Span attributes worth knowing: the root carries `session.id` (the agent id)
and `meiva.turn` (the per-process turn counter); children carry
`traceloop.association.properties.langgraph_node` / `.langgraph_step` /
`.langgraph_path`, `traceloop.association.properties.ls_model_name`, and
`gen_ai.operation.name`. The root records the turn's duration, input
span links and, when available, `meiva.checkpoint_id`. If the process dies
mid-turn, children can reach Tempo before the root is exported.
`read_trace.py --with-content` resolves the checkpoint through
`checkpoints.metadata.trace_id` using the gateway trace endpoint. Check a real
span before querying auto-instrumentation attributes, since a library upgrade
can rename them.

## Fetch

The three scripts below live in a Meiva source checkout, `.agents/skills/inspect-a-trace/scripts/`
(`<scripts>` here), and run with that checkout's `.venv`. Away from a checkout,
`gq tempo/api/traces/<hex-32>` fetches the same whole trace.

```bash
# find first — search returns trace ids
.venv/bin/python <scripts>/fetch_trace.py --search '{ resource.service.name = "meiva" && span.traceloop.association.properties.agent_id = "3048" }'

# then pull the one trace from Tempo's full-trace API; it fails above 5 MB.
.venv/bin/python <scripts>/fetch_trace.py --trace-id <hex-32> --out trace_raw.json
```

`fetch_trace.py` needs only network access to the Tempo query URL; from another
machine, point `--tempo-url` at it or at the Grafana datasource proxy
(`/api/datasources/proxy/uid/tempo/...`).

## Read

```bash
.venv/bin/python <scripts>/read_trace.py trace_raw.json --out trace_read.json \
  [--with-content] [--with-events] [--gateway http://localhost:8000]
```

Produces the span tree (parent/child, ordered), per-span offsets and durations,
the LLM span list (names ending `.chat`), the langgraph node sequence, and the
root's `session.id`. `checkpoint_id` comes from the root's `meiva.checkpoint_id`
on pre-#1964 traces; on newer traces the root no longer carries it — pass
`--with-content` to resolve it from the checkpoints table via the gateway.

- `--with-content` fetches the messages from
  `GET /api/agents/{agent}/traces/{trace_id}/messages` — the checkpoint's full
  message list, system prompt included. `pruned: true` is the expected shape
  for an old run whose checkpoint was trimmed, not an error.
- `--with-events` fetches the correlated event rows via
  `GET /api/events?trace_id=<hex>&from=<ISO>` (`events`, with `events_total`): every telemetry
  and log event of the turn within Loki's 7-day window (the gateway reads them from Loki) and
  its audit events, which Postgres keeps permanently. A trace older than seven days has only its
  audit events; nothing else covers what Loki missed.
- Gateway auth: bearer `MEIVA_API_TOKEN` (a launched process's machine token),
  else `MEIVA_CLUSTER_SECRET` from the environment or the gateway's
  `$MEIVA_HOME/.env`. The gateway is port **8000** (8100 is an unrelated service).

## Render

```bash
.venv/bin/python <scripts>/render_trace.py trace_read.json --out report/
```

Writes a self-contained `report/index.html` — inline CSS/JS, no CDN, no build
step, opens offline. Header, waterfall timeline, node sequence, LLM span table,
events, message content when present, and a Grafana Explore link. Hand over the
`report/index.html` path.

The report and Grafana are complements: Grafana is the interactive browser, the
report is the sharable annotated artifact. Prefer just handing over the Grafana
link unless the annotation is the point.

## Pitfalls (observed on Tempo 3.0.2; not re-checked on 3.1.1)

- **5 MB full-trace cap** — `/api/traces/{id}` refuses larger traces outright.
- **`spss` caps at 100, and time-window paging is unreliable** — a window can
  come back empty while the trace exists outside it. "Reassemble a whole trace
  from search" is not a thing; fetch by trace id.
- **Regex `=~` on the `name` intrinsic misbehaves** — anchored patterns
  (`"^goto"`) match nothing, and `name =~ ... && <anything>` returns empty.
  Use exact `=` on names; `=~` on span attributes is fine.
- **Ids are hex in Tempo** — 32/16-char hex. The base64 form is the OTLP/JSON
  spec's and appears only in legacy envelopes; the scripts handle both, by
  hand it is `bytes.fromhex(s)` (or `base64.b64decode(s).hex()` for a base64
  id). Never paste one form into the other.
- **`/api/v2/search/tag/*` only enumerates intrinsics** (name/kind/status), not
  attributes — it is not a schema explorer.
- **`search` returns only the attributes you queried** on its spans; the full
  set comes from the full-trace endpoint (`fetch_trace.py --trace-id`).

Ingest is the sidecar relay only. Never point a write path at Tempo directly.
