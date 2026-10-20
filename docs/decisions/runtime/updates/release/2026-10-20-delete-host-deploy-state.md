# Delete the host deploy-state posture

## Context

`host_deploy_state` held one row per machine with an `idle` or `paused` posture
(`base/deploy/state/`). `meiva stop`, the pause fan-out and the maintenance hold wrote
`paused`; `meiva start` wrote `idle`. Three kinds of reader consumed it: the gateway's
503 middleware, the deploy window that told a transition from an outage, and
`meiva status` with its `paused_reason`.

The first two are gone. [The maintenance hold](../../processes/2026-10-20-delete-maintenance-hold.md)
and its 503 middleware are deleted, and
[the deploy-window readers](2026-10-20-delete-deploy-window-readers.md) were removed
before it. Once the hold no longer wrote `paused`, the table had a single writer, the
tail of `meiva start`, and a single reader, the status clause. The row said "idle"
about every host that had ever been started and "paused" about none, so the
`business_pause` verdict could not fire.

## Decision

- Drop the table with a forward migration, remove it from `db/schema.sql` and from the
  runner's write grant (`machines` stays), and delete `base/deploy/state/` and
  `ops/cluster_status/pause.py`.
- `meiva start` writes no posture. A stop writes none either; it clears the serving
  gate, which is the one fact a status reader needs.
- The status verdict `paused` keeps two clauses. `no_state` now means "this host's
  database snapshot could not be read", answered by an explicit `db_ok` flag instead of
  the absence of a row; `startup` means "the serving gate has not reached `serving`".
  The API type `PausedReason` loses `business_pause`.
- A pure gateway machine reads the same gate: its `paused` is `not
  start_serving.is_serving()` and its reason is `startup`, where it used to read the
  posture row.
- The two earlier migrations that alter or comment the table
  (`20261001T055030_drop-retired-deploy-and-watcher-storage`, `20261017T120000_meiva-rename`)
  are stamped in the baseline's seed, as the baseline already holds the rest of their
  result; a fresh database does not replay them against a table the baseline no longer
  creates.
- The migration is not expand-contract: it ships with the code that stops using the
  table, in a stopped cluster, as the host-fencing storage retirement
  (`20261020T073712_retire-host-fencing-storage`) did.

## Failures that no longer exist

| Failure | Why it is gone |
|---|---|
| A posture row that nothing returned to `idle` (#2160: a machine left at `paused` for 31 hours) | There is no row. |
| A status reading `business_pause` for a host that had only been stopped | The clause is deleted; a stopped host reads `startup`. |

## Consequences

- `GET /api/cluster/status` and the roster no longer return `business_pause`; a consumer
  that matched on it sees `startup` or `no_state` instead.
- Rows in a previous release's table are dropped with it. Nothing carried them.

Forward link: completes the removal that
[the deploy-window readers](2026-10-20-delete-deploy-window-readers.md) deferred ("the
`host_deploy_state` table, its writers and the `meiva status` clause stay").
