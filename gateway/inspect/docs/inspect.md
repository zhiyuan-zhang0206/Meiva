# Agent Inspect Router

`/api/agents/{id}/inspect/statistics` + `/inspect/live` + `/neighbors` — per-agent LLM cost/token usage + neighbor graph.

The live inspector's heartbeat block — the four display states (a pause, a queued wake,
a projected check-in, nothing) and the daemon predicate each one mirrors — has its own
node: [Inspector heartbeat projection](heartbeat.md).

Statistics read LLM usage and price snapshots from the Postgres usage ledger.
The response carries API list-price USD, virtual subscription USD, calls without
a price and incomplete usage reports. Cache-hit rates divide reported cache
reads by input from reports that include cache metering. A window with nonzero
input and no cache metering returns a null cache-hit rate.

The statistics path serves only the explicit hour windows whitelisted by `StatsWindowHours`
(0/1/6/24/72/168/720) and cumulative-since-spawn; turn counts, exec outcomes, durations, TPS and
active time are not served (they came from persisted observations of `telemetry_events`, which
has no consumer and no writer left). The `/inspect/live` half remains
window-independent — see [Ops Surfaces (cluster admin + stats dashboard)](../../cluster/docs/ops-surfaces.md).

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
