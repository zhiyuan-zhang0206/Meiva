# Alert-pipeline host sentinel

The sentinel queries Prometheus for the age of the newest accepted alert ingest.
It remains unarmed until it sees its first sample. After that, a stale or absent
heartbeat triggers a silent-pipeline notification through `meiva agents send`.
An unavailable endpoint or invalid response triggers a separate query-failure
notification and returns nonzero. The two conditions have independent persisted
repeat windows. Failed submissions stay retryable. A lock covers the read,
submission and receipt so overlapping ticks submit at most once.

A planned update holds the sentinel. `meiva update down` and `up`
(`cli/fleet_update/fleet_alert_silence.py`) keep the end of the window's Grafana
silence in `<home>/ops/alert-window-until` (one UTC ISO timestamp: `down` writes
the requested expiry, `up` shortens it to the 10-minute settle grace). Until that
instant a tick only logs `held planned update window` and neither queries nor
notifies, on both legs, because the update stops the gateway, Prometheus and
Grafana on purpose and the heartbeat series needs a webhook delivery after the
restart to come back. The first tick after the window judges the pipeline as it
is; a failure that outlives the window is reported then. The marker is a file
because Grafana, which owns the silence, is stopped for most of the window. A
missing or unreadable marker holds nothing.

The existing `watchdog-sentinel.log` records completed Prometheus observations,
including valid empty vectors and held ticks. Its freshness monitor therefore sees the age of
the last observation. Query failures, overlapping runs and submission
diagnostics go to the adjacent `watchdog-sentinel.diagnostics.log`.

Delivery requires the installed CLI and the cluster's agent messaging services.
If the gateway rejects the message, the local error and nonzero exit remain
available and the sentinel retries on the next tick.

Run one tick with `python scripts/host_ops/watchdog_sentinel.py`. Use `--dry-run`
with a temporary home to inspect its decision without submitting a message or
changing the live receipts.
