# Process database gate

`base/db/code_version_gate.py` defines `ProcessDbGate`, which owns the process database
identity and session restore configuration across pooled Postgres sessions.

## Code version & Application name

`base/native_process/code_version.py` (standard library only): the number of
first-parent commits reachable from the commit the process loaded
(`git rev-list --count --first-parent <sha>`), from `loaded_commit`'s boot capture
and never from the checkout as it later became, cached for the process lifetime.
A sealed release tree carries the count in its `.meiva-release.json`, computed by
the stager from a full clone, and the capture keeps it, so a release needs no
history. A tree that is neither raises `CodeVersionError`; nothing substitutes
`0`.

`pool()`, `async_pool()` and pooled `connect()` start every connection with
`application_name = meiva:<process>:v<version>` (`ProcessDbGate.application_name`),
resolving the version at construction so a tree without git fails there, on the
caller's thread. PgBouncer's `SHOW CLIENTS` lists these names.

## The session restore schedule

`base/db/connections.py` runs the baseline-session restore on every pooled dial,
pool-backend creation and borrow (`_restore_pooled_session` and its async twin).
Its statements re-apply the statement ceiling and the connection's
`application_name` that `RESET ALL` cleared; on a borrow where
the retained `ProcessDbGate.min_read_due()` (the process's first, then at most every
`MIN_REFRESH_INTERVAL_S`), `observe_minimum` updates the last read timestamp.

## Exemptions

- Direct connections (`direct=True`: administrator, migration applier, `pg_dump`) and
  `connect_url` targets. They carry no name.
- The `meiva` CLI: one `cli.database.OperatorDatabaseFactory` retains an explicitly
  exempt gate for its command, because `meiva stop` must still drain agents on a
  stale host. Its ordinary and scratch-URL builders share that owner and dial as
  `meiva:cli` without resolving Git. Service entries retain their own nonexempt gates.

Tests: `base/native_process/tests/test_loaded_commit.py` (the count, on real repositories),
`base/db/tests/test_connect_helpers.py` (the read schedule, the restore against a fake and a real Postgres),
`tests/components/cli/test_pgbouncer_wire.py` (the name in a real PgBouncer's client list).

Parent: [infrastructure utilities](../../docs/infrastructure-utilities.md).

## Explicit process composition

`ProcessDbGate` is the gate owner supplied by an executable entry point. The
entry retains one instance across its independent `Database` handles, lazy
handle factories and sync/async pools. A handle binds that owner; creating or
refreshing a handle does not restart its 30-second budget. Pool configure and
check callbacks retain the same owner and keep the restore statement schedule.

The entry supplies the version reader of its captured loaded image, the existing
process label and the CLI exemption posture. Version resolution remains lazy;
a service's reader must resolve the captured SHA, never the checkout's later
HEAD. An exempt CLI does not resolve a Git version to name or restore a session.
