# Delete the deploy-window signal and the machine exclusions

## Context

`ops/deploy_window.py` answered "is any machine mid-deploy?" from every machine's
`host_deploy_state` posture row. It was built so health-probe alerts, the updater,
the pin and code controllers and the heartbeat could tell a transition from an
outage. Those readers were deleted with the deployment lease, the updater and the
alert grading (see [remove the deployment lease](2026-09-30-remove-deployment-lease.md));
alerting is now silenced by Grafana. The only production caller left was the daily
WAL-G tick (`services/backup/walg/tick.py`), which skipped when the signal was
active.

`base/cluster/machine_exclusions.py` existed for one reader of that signal. In
#2160 a machine paused by the operator left its posture at `paused` for 31 hours,
and the signal refused every cluster update until `--force`. The failure was the
signal itself: a posture row that nothing returns to `idle` while the machine is
excluded. The exclusion list taught the signal to ignore such rows. Its only
caller was `ops/deploy_window.py`.

## Decision

- Delete `ops/deploy_window.py` (including `deploy_in_flight`), `base/cluster/machine_exclusions.py`
  and their tests.
- The WAL-G tick keeps one skip gate: Postgres does not accept connections. That
  is the fact the backup needs, and `_skip_reason` already checked it. The deploy
  window gate was wider than that fact (any machine in the cluster, while the
  backup only concerns the gateway's Postgres) and did not close the race it
  appeared to (Postgres stopped mid-backup).

## Failures that no longer exist

| Failure | Why it is gone |
|---|---|
| #2160: an excluded machine's stale `paused` row blocks a cluster-wide update | Nothing reads the posture row for a cluster-wide verdict, so there is no verdict to be wrong. |
| A runner's deploy skips the gateway's backup | The tick asks only whether Postgres accepts connections. |

## Not changed

The `host_deploy_state` table, its writers and the `meiva status` clause stay;
they are removed with the maintenance hold in a later change.

## Alternatives rejected

- Keep `deploy_window` for WAL-G alone: it needs the exclusion list and a table
  read to answer a question Postgres answers directly.

Forward link (2026-10-20): the `host_deploy_state` table is dropped with its owner module; see [delete the host deploy state](2026-10-20-delete-host-deploy-state.md).
