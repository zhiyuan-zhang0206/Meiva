# Built-in schedule provisioning

`schedules/manifest.json` is the single expression of the built-in policy: a
`product` schedule ships enabled, an `operator` schedule ships disabled and waits
for the operator to enable it. Provisioning reads that manifest and touches one
row per entry:

- a missing row is created with the manifest's `default_enabled`, plus the same
  `initial` version snapshot the API create writes, the manifest's `cron` or
  `every` trigger, and its first `next_launch_at` when enabled;
- placement policy determines target machines: `placement: "gateway"` (the default) assigns
  the row to the gateway machine, while `placement: "per_machine"` (such as `harness-cli-update` and `claude-auth-keepalive`)
  creates a row `<name>:<machine>` for every machine in the cluster with the `agent-runner` role,
  queueing sync requests to start or update them;
- a row whose script, command, trigger, timezone, or timeout differs from the
  template is resynced to it,
  writing a `schedule_versions` row (`builtin-resync <hash>`) and queueing a
  `schedule_sync_requests` row. A legacy `daemon` row is upgraded in place; the
  request reaps its old PTY before declarative admission is allowed;
- operator-owned values stay: `enabled` and `description`, so
  `meiva schedules stop <name>` keeps a built-in stopped and a deleted built-in
  returns with the manifest default. Provisioning derives status and
  `next_launch_at` from the preserved enabled state and the declared trigger.

The checkout is the source of truth — a hand edit of the stored script is
replaced at the next provision, so change the template instead. Agent-created
schedules are not in the manifest and are never read or written here. Both call
sites (the gateway lifespan and `meiva schedules provision`) pass their own
connection, and provisioning is idempotent against an unchanged checkout.

The operator-facing table and the deploy procedure live in `schedules/README.md`;
the start-time seeding flag lives in `gateway/schedules/docs/schedules.md`.
