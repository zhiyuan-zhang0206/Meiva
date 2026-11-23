# Alert-pipeline host sentinel

The sentinel queries Prometheus for the age of the newest accepted alert ingest.
It remains unarmed until it sees its first sample. After that, a stale or absent
heartbeat triggers a silent-pipeline notification through `meiva agents send`.
An unavailable endpoint or invalid response triggers a separate query-failure
notification and returns nonzero. The two conditions have independent persisted
repeat windows. Failed submissions stay retryable. A lock covers the read,
submission and receipt so overlapping ticks submit at most once.

The existing `watchdog-sentinel.log` records completed Prometheus observations,
including valid empty vectors. Its freshness monitor therefore sees the age of
the last observation. Query failures, overlapping runs and submission
diagnostics go to the adjacent `watchdog-sentinel.diagnostics.log`.

Delivery requires the installed CLI and the cluster's agent messaging services.
If the gateway rejects the message, the local error and nonzero exit remain
available and the sentinel retries on the next tick.

Run one tick with `python scripts/host_ops/watchdog_sentinel.py`. Use `--dry-run`
with a temporary home to inspect its decision without submitting a message or
changing the live receipts.
