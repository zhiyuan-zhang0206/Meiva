# Agent Inspect Router

`/api/agents/{id}/inspect/statistics` + `/inspect/live` + `/neighbors` — per-agent LLM cost/token/TPS + neighbor graph.

The live inspector's heartbeat block — the four display states (a pause, a queued wake,
a projected check-in, nothing) and the daemon predicate each one mirrors — has its own
node: [Inspector heartbeat projection](heartbeat.md).

Statistics read LLM usage and price snapshots from the Postgres usage ledger.
The response carries API list-price USD, virtual subscription USD, calls without
a price and incomplete usage reports. Cache-hit rates divide reported cache
reads by input from reports that include cache metering. A window with nonzero
input and no cache metering returns a null cache-hit rate.

Turn and activity fields use persisted observations and day summaries.
Pre-cutover full days use the original turn ledger. Exact observations feed
duration quantiles, and historical histograms carry integer-second precision.
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
