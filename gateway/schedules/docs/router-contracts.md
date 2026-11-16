# Schedule router contracts

The HTTP creation boundary accepts `at`, `cron`, and `every` triggers. A caller
supplies either an ordinary Python script or a wake message with a target. Message
creation stores the minimal Python delivery program, so every execution follows the
same process contract. The router syntax-checks the resulting script before writing.

Trigger payloads are closed forms: one `run_at`, one cron expression, or one interval
of at least 30 seconds. Cron validation uses the same timezone-aware implementation
as the runtime. Cluster timezone and a 600-second timeout are defaults at acceptance,
making retries reconstruct the same response from the receipt's original timestamp.

`owner_agent_id` is accepted on create and update. Unless a machine is explicit, an
owned schedule follows the agent's recorded machine; a system schedule stays on the
accepting gateway machine. Owned schedules default to cancellation when the owner
terminates. Foreign keys and missing machine placement surface as request errors.

List projections omit script bodies; full reads retain them. Script or command edits
remain versioned. Declarative edits apply on the next fire, while daemon edits retain
their revision/session convergence path. A declarative machine move or deletion is
accepted only while no run is open, preserving one manager and one workspace as the
owner of an in-flight process.

Lifecycle verbs share durable, idempotent mutation receipts:

- `enable`/`start` compute the next declarative fire or start a daemon;
- `disable`/`stop` prevent future fires and ask reconciliation to stop active work;
- `run-now`/`restart` make one declarative fire immediately eligible or relaunch a
  daemon;
- `cancel` records the terminal lifecycle intent.

Declarative log reads return `latest.log` from the schedule's locality. Daemon reads
retain live capture and saved PTY transcript fallbacks. Both use `last_error` only
when no execution output is available. Remote reads dispatch to the target machine's
ops service with the trigger and owner locality facts.

Schedule values remain bound SQL parameters. Interpolated projections are fixed
module constants, and every mutation locks or creates its target before relying on a
non-null returned row.

## Relationship to other nodes

- [Schedule Manager & Runner](schedules.md) — execution and recovery semantics.
- [Schedule mutation receipts and revision convergence](schedule-convergence.md) —
  the daemon compatibility receipt protocol.
