# Built-in and plugin schedules become declarative

## Decision

Every schedule in `schedules/manifest.json`, plus each `meiva_usage` collector,
uses a declarative `cron` or `every` trigger and a run-to-completion Python
entry. Recurring cadence, timeout, single-flight admission, and run history
belong to the schedule manager. State that spans rounds is an ordinary
workspace file.

## Compatibility transition

Provisioning upgrades a same-name `daemon` row in place. The row keeps its id,
history, description, and operator-selected enabled state. Code and trigger
policy follow the manifest or plugin declaration. The resulting sync request is
also a launch barrier: the declarative engine admits no due run while any
request for that schedule remains, and the compatibility manager reaps the old
`meiva-schedule-<id>` PTY without overwriting the row's scheduled status.

This preserves rollback evidence and avoids a duplicate row or an interval in
which the resident and single-round forms execute together.

## Stateful rounds

The database-stall and QA watches persist their full streak/window state between
processes. Main-red and agent-watch move their state into their schedule
workspace, copying the former `$MEIVA_HOME/run/` file on first use. Weekly
self-evolution persists its Tuesday-to-Friday follow-up arm. `meiva_usage`
persists consecutive provider rate limits and a `not_before` time, allowing its
fixed `every` trigger to retain exponential backoff without a sleeping process.

## Follow-up

The `daemon` execution path remains for stored external schedules. Its removal
is a separate compatibility decision after those rows have migrated. This
implements the installed-payload follow-up recorded in
[Phase 1A](declarative-schedules-phase-1a.md).

## Update: migration barrier survives cleanup alerts

The bounded give-up policy for ordinary compatibility daemon convergence does
not acknowledge an in-place daemon-to-declarative migration. Its sync request
remains durable and retryable after `schedule_convergence_abandoned`, so the
declarative engine stays fenced until the old PTY reports a definitive reap or
absence result. The alert bounds one failure streak; it does not turn uncertain
legacy-process custody into successful cleanup.
