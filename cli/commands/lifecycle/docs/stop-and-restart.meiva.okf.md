---
type: doc
title: Stop and restart
description: How `meiva stop` and `meiva restart` plan, execute and report a stop, and what each leg of a restart keeps.
tags:
- cli
- lifecycle
- stop
---

# Stop and restart

`stop/` is a plan and a shell. The operator's answers (`choices.StopChoices`: shell
sessions, browser, extras, data plane, other services, whether to announce, force) and the
host facts the shell read once (`choices.StopHost`: the capability set, the service roster,
which units need the database, the pty-sessions unit, whether the home is a proven
unstarted attempt) go into `plan.plan_stop`, which returns the ordered steps
(`steps.StopStep`) and the scope the confirm gate shows. Every keep-or-stop choice, the mode
and the budget are explicit values with no default. Every contradiction is a `ValueError`
raised by the plan before one unit is signalled: a data-plane choice the host's capabilities
do not match, `--keep-service` naming the shell-session or browser unit (they have their own
choices), an unknown or database-dependent kept service while the data plane stops, and
`--stop-extras` together with any keep choice (`plan.require_consistent`, the same words the
fleet's parser uses).

There are three shapes. A normal stop revokes serving, announces, stops the services with the
pty-sessions unit kept, stops the browser, closes the shell sessions through that service,
then stops the unit itself, the extras and the data plane, in that order; it never escalates a
service or data-plane stop. The close of a proven pre-application attempt
(`evidence.require_unstarted_initialization`: an identity journal still in its `configured`
phase and no serving, manifest, root or shell session) announces and drains nothing. An
explicit force interrupts the services, reaps a Chrome that escaped the browser session and
closes the shell sessions with no grace and no notices; it reads no service roster (its ports
derive from the settings) and so stays usable when those cannot be loaded. `execute` performs the steps one at a
time, timing each phase of a normal stop into the lifecycle journal; `render` words the scope,
the skipped data plane and the failure report; `admission.confirmed` is the y/N gate over the
`stdin` stream the command entry hands in. The caller's `retained_children` list travels as an
argument beside the frozen `StopEnvironment`; `execute_plan` returns the `StopOutcome` (escalation
notes, pooler clients) that `report.finish_stop` journals, built from the `MaintenanceStop` value
`maintenance_stop.stop` returns. Evidence that cannot be read fails before the journal opens and is
reported like any incomplete stop (exit 1, no journal row).

Restart over the service tree runs four legs (`restart_plan.Leg`): the preflight probes and
`start/readiness_preflight` (both refuse before anything stops, with the decline exit code
[[start-readiness-preflight.meiva.okf.md]]), the stop leg and the start leg.
`restart_plan.settle_leg` says what each result means; `restart_stop_request` is the stop leg
(the data plane kept on a gateway, no announcement, no prompt, the operator's other choices) and
`restart_start_leg` its start leg (the operator's explicit service selection, not reconciled to
the units the stop kept). `restart.restart_runtime` admits the checkout once, before the
journal opens, and the `StartRuntime` it returns is held through both legs; the start leg re-reads its source digest when it publishes serving. `meiva restart --only agent-host` first admits the live root
(`restart.admit_agent_host_restart`, before the roster is built), then downs agent-host through
it and brings it back (`restart.restart_agent_host`), touching nothing else.

The stop does not drain agents itself: root stops agent-host first and waits for it,
and its own SIGTERM exit settles the running turns
([[services/agent_runner/agent_host/lifecycle/docs/graceful-exit.meiva.okf.md]]).
`stop/shell_sessions` and `data_plane/maintenance_stop` verify
resource exits (`data_plane/maintenance_stop_report` names survivors). What the shell-session
closure's outcome means (owner notices first, then the leftover jobs, then a failing shell or the
wait for an empty service table) is `closure_plan`'s; a survivor role other than the closure's two
(`closure_plan.SurvivorRole`) is a `ValueError`, never a job; the deadline arithmetic is
`data_plane/stop_budget`'s. Each wait spends the `base.clock.Deadline` it was handed.
`data_plane/_pooler_stop.OwnedPooler` owns ordinary pooler stop
admission for maintenance and startup recovery: exact native birth and listener
proof precede a durable stop intent and the first SIGINT (`WAIT_FOR_SERVERS`).
Retries and already-closed listeners only wait; PgBouncer would interpret
another shutdown signal as immediate termination. A birth that exits while its
listeners are scanned is stopped, not foreign. An explicit force request alone permits a kill, with a separate
bounded settle wait when the graceful deadline is spent. The data-plane stop
requests [owned PostgreSQL](../../../../base/cluster/docs/postgres.meiva.okf.md) fast
shutdown (SIGINT), so neither waits on idle client connections a drained state
cannot protect (issue #2307). When the data-plane phase fails after the
services phase stopped, `stop` reports the failure with its phase accounting
(`report.report_incomplete`) and exits non-zero; it does not restart anything, and the operator
decides whether to retry the stop or run `meiva start`. A force stop ends the same way: the
cluster instance stop reports a Redis that still answers through its return code, which is
a failed step (`force_outcome.data_plane_failure`), and the stop prints
`force_outcome.force_incomplete_report` and exits non-zero rather than reporting success. Any
other failure of a force step (a root it cannot reach) stays the command's own: a force stop has
no journal to report into.

`stop/extras` uses the same exact-home helper retirement as destroy: native
job, executable, socket and direct root-child ownership are checked before native
helper exit and removal of its definition. Start recreates that definition. The helper exists
on macOS only (`extras_plan.retires_helper`) and cannot be retired while any unit is retained.
Root owns Gate and native LGTM application services. `do_stop` and `cmd_start_body`
hold the home lifecycle lock for a whole start or stop.

A stop keeps no state that gates anything: `stop` clears the home's serving gate
(`base/deploy/lifecycle/start_serving.py`) before it stops anything, `start` marks it
serving after readiness, and `run/lifecycle-op.json` records how a stop or restart
ended for the fleet update and the operator.
See [the coordinated operator procedure](../../../../docs/conventions/operations/graceful-maintenance.md).

The native stop tests create fresh PTY sessions and wait for the requested job's bare readiness
output (`job-ready`, or `stubborn-ready` once a stubborn job has installed its handlers) before
treating a shell session as busy: a login-profile child can exist before the initial command runs,
so child presence is not readiness. `cli/commands/lifecycle/tests/stop/stop_support.py:busy_session` keeps cleanup custody. The plan tests assert the order of the steps, the real-shell tests assert
the closure, and the notices a stop wrote are read back from the suite's Postgres, as are audit
rows (`cli/database/tests/lifecycle_rows.py`). A gateway's cold preparation runs in a fresh
interpreter (`tests/start_storage/test_gateway_cold_start.py`), because its birth adopts a database
login into the process environment.

The stop tests' private non-PTY sessions run `tests/stop/_child.py`, which takes only SIGTERM
behavior, an optional ignored SIGHUP and an optional armed-marker path, as argv data. The ready marker follows signal setup, and the fixture records the native session
identity before returning and closes that exact private group and pipes when the test exits,
assertion failure included.
