# Fleet update up half and completion gates

`meiva update up` brings a cluster back after `down` switched every checkout to `NEW` (either inside `meiva update run` or as a standalone half after manual release work or recovery).

## Steps, in order

1. `meiva start` with the operator's service selection on the gateway first. Confirm its authenticated `/api/bootstrap` response matches the built-in plugin field declarations before starting all runners in parallel. On macOS, start hosts through a run-once `gui/<uid>` LaunchAgent, because the helper's launchd job lives in `gui/<uid>`. Each start must exit 0. A gateway whose root was kept across the window (`--keep-shell-sessions`) applies the pending migrations in that live start; a gateway whose root was stopped applies them in its cold start.
2. The roster gate: every listed machine online with checkout and running code on one commit; unlisted machines are reported and never block.
3. `meiva packages refresh` across all hosts in parallel; it never forces.
4. The drift checks, last, in parallel: `meiva schedules verify --no-notify` on the gateway, `meiva plugins verify` on every host and `meiva harness verify-sessions` on every host (a session record the code cannot read, such as one left in an older layout because the start did not migrate it, fails the half). Every check runs to the end and a red fails `up` with all the details — after the refresh, so a red never skips it.
5. Expire the silence.

## Completion contract

Before `up` completes: every listed machine is online on the roster with checkout and running code on one commit — re-read on the poll interval until `--roster-timeout`; a listed host is matched by the machine name it reports, a name with no row fails, unlisted machines are reported and never block; and every host ran `meiva packages refresh`, which replaces a differing local copy and never forces.

A failure of the gateway's start (a migration included) ends the half with the cluster stopped: nothing rolls back, because the new start may already have migrated the database, the summary names the gateway and the whole half is rerun (`up` never repeats `down`). A runner failing its start or a check is that host's line: the others still start and verify, runners the window deferred in `down` are not attempted, and the half exits 3 with `fleet-update-result:` naming them. Refusals exit 2 (nothing touched); other failures exit 1.

Parent: [Fleet update over SSH](../fleet-update.md).
