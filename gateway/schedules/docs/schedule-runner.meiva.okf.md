---
type: doc
title: Schedule Runner — the in-session entrypoint
description: The process a schedule session runs — loads the schedule, executes its script under a stall guard, and records terminal state the schedule manager reads.
tags: []
---

# Schedule Runner — the in-session entrypoint

Launched by the `schedule-manager` service ([[schedules.meiva.okf.md]]) inside the session `meiva-schedule-<id>`.

- **In-session entrypoint**: `.venv/bin/python -m services.wake.schedule_manager.runner <id>`
- Loads the schedule's script + command from the DB
- Materializes the script to `$MEIVA_HOME/schedules/<id>/`
- Binds the `schedule:<id>` actor identity (so `meiva.agents.*` invocations are attributed to the schedule)
- `.py` scripts are executed in-process via `runpy`; other commands are run as subprocesses
- **Stall guard**: a deepest non-park frame stable beyond the `StallPolicy` timeout (the runner reads `schedule_stall_timeout_seconds`, default 1200s, once at start; sampled a quarter of it apart, at most every 30s) triggers cleanup. The verdicts are pure functions (`stall_policy`, `guard_admission`). Wrapped sleep/sleep-family waits, subprocess `_wait` and selectors' `select` directly inside `_communicate` park; caller `timeout=` bounds child waits when supplied. Spawn, conversion, stdin flush and other selectors stay guarded. Leaving a park resets the budget. A non-`.py` command is bounded by the same timeout as a wall-clock limit.
- **Hard-exit cleanup**: capture descendants and verify birth identity + current ancestry. TERM, 3s grace, KILL survivors (recheck identity), reap up to 5s. Runner/shared PTY group excluded; `setsid()` covered. Already-reparented daemons, pre-signal ancestry/identity mismatches and later births are exempt. Then one `faulthandler` deadline (`StallPolicy.record_deadline_s`, 10s) is armed (it dumps every thread's stack and exits 1 if a DB write never returns), `last_error` and the run row (`ok=false`, `stalled (<n>s)`) are written, and the process exits 1. The exit is terminal process loss: the manager records an error and does not relaunch, and the `schedule_stalled` alert fires after two sessionless hours.
- **Exit means terminal**: script exits cleanly with rc=0 → runner writes `status='completed'` before exiting (the resident process finished, manager will not restart); non-zero rc / uncaught exception → traceback written to `schedules.last_error` (latest crash) and, tail-truncated to 3000 chars after a `crashed: <ExceptionName>` first line, to that run's `schedule_runs.note` (one per crash; the session log is gone with the session), reported as terminal process loss; explicit start/restart is required; SIGTERM/SIGHUP active kill → nothing written, not counted as a crash

The module entry passes arguments after the program name explicitly to `main`.
That entry uses the actual home checkout guard before composing SDK resources;
a home containing its own `source` directory refuses a foreign checkout. Tests
create that real directory in their isolated home instead of replacing the guard.

The process entrypoint (`runner.run`) is the composition root: it statically imports
the SDK, binds the schedule's actor, and hands `services/wake/schedule_manager/engine.py` one
`RunnerRoot` of values: the database, the `ScheduleInputs`, the plugin-loading inputs, the
`StallPolicy` and the home. The engine reads no settings and takes no callbacks. It captures the
SDK's loaded image and gives execution and lazy telemetry handles one code-version
gate. `runpy.init_globals` supplies immutable `ScheduleInputs` to the templates;
their execution bodies borrow the same builders without constructing or closing
another gate or writer. Direct Python template execution owns its separate entry.
Configuration reads the launcher's already-delivered environment without
redelivering it. The process closes its actual clients with a two-second pipeline
drain budget, preserving an execution error when cleanup also fails. The actor is bound
before the engine materializes the script and opens the run-history row. SDK
policy readers retain their normal live read timing; the gateway HTTP process
does not construct this schedule SDK owner. The manager launches the new entrypoint
for every new session. Existing schedule processes keep their original code;
the manager recognizes their recorded revision and adopts matching live
sessions without reaping or replaying them.

The guard starts before plugin loading. Script completion closes stall admission
under the same lock used to claim a real stall, wakes the guard, and joins its
actual thread before restoring sleep or writing terminal outcomes. A sample
already in progress cannot admit a new stall after close. An action admitted
before close still reaps descendants and hard-exits; normal completion never
overtakes it. Ordinary close has a 1s join budget. After stall admission, the
finite guard join budget covers the 3s TERM grace, 5s reap and the record
deadline with 1s scheduling margin. If that join expires after a real stall was
admitted, the runner still exits 1 without terminal bookkeeping.

The guard thread retains its original unexpected error, logs it immediately and
raises it from `close`; any frame-read failure is such an error (there is no
skip-and-continue path). A failed script remains primary when guard close also
fails, and a successful `SystemExit` (`0`, `None` or `False`) is completion that
cannot mask a guard close failure or produce a completed marker. Severable
run-record write failures are logged.

## Execution-engine test boundary

`gateway/tests/extensions/test_schedule_runner.py` uses the real test database
for the execution engine, in this process, with the policy a test chooses; a write that
must fail is made to fail in the database by a trigger. The stall guard's verdicts are
asserted on values (`services/wake/schedule_manager/tests`), what it observes is asserted
on real frames (`gateway/tests/extensions/test_schedule_stall_guard.py`), and the hard exit
with its cleanup, bounded record writes and completion race run in a real runner process
against row locks the test holds (`tests/integration/test_schedule_runner_cleanup.py`).
