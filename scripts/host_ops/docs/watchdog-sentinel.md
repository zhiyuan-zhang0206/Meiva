# Alert-pipeline host sentinel

The sentinel queries Prometheus for the age of the newest accepted watchdog heartbeat (the gateway's alert webhook):
`time() - max(max_over_time(meiva_alerts_last_ingest_timestamp_seconds[1h]))`. The
range selector is what makes the age continuous across a gateway restart, which
replaces the series and leaves the new process without a sample until the next
watchdog delivery (up to about ten minutes later); an instant selector reads that
gap as an absent series. The one judgement is that age against a budget of 1800 s:
the watchdog route delivers every five minutes, a restart adds up to ten, the
longest age measured across two rolling releases on 2026-11-24 was 945 s, and
thirty minutes keeps a margin of nearly 2x and one missed 15-minute tick. No sample
at all in the hour is an age beyond the budget. Before the first heartbeat has ever
been seen there is nothing to age, so the sentinel stays unarmed until it sees its
first sample. After that, an age beyond the budget triggers a silent-pipeline
notification through `meiva agents send`.
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
