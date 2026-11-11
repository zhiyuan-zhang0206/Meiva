# Fleet update up half and completion gates

`meiva update up` brings a cluster back after `down` switched every checkout to `NEW` (either inside `meiva update run` or as a standalone half after manual release work or recovery).

## Steps, in order

1. `meiva start` with the operator's service selection on the gateway first, then across all runners in parallel; on macOS through a run-once `gui/<uid>` LaunchAgent, because the helper's launchd job lives in `gui/<uid>`. Each start must exit 0.
2. The roster gate: every listed machine online with checkout and running code on one commit; unlisted machines are reported and never block.
3. `meiva packages refresh` across all hosts in parallel; it never forces.
4. The drift checks, last, in parallel: `meiva schedules verify --no-notify` on the gateway, `meiva plugins verify` on every host and `meiva harness verify-sessions` on every host (a session record the code cannot read, such as one left in an older layout because the start did not migrate it, fails the half). Every check runs to the end and a red fails `up` with all the details — after the refresh, so a red never skips it.
5. Expire the silence.

## Completion contract

Before `up` completes: every listed machine is online on the roster with checkout and running code on one commit — re-read on the poll interval until `--roster-timeout`; a listed host is matched by the machine name it reports, a name with no row fails, unlisted machines are reported and never block; and every host ran `meiva packages refresh`, which replaces a differing local copy and never forces.

A failure ends the half: nothing rolls back, the message names the step to fix, and the whole half is rerun (`up` never repeats `down`). Refusals exit 2 (nothing touched); failures exit 1.

Parent: [Fleet update over SSH](../fleet-update.md).
