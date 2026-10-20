# Stop, restart, and resume a cluster

Use the `meiva` belonging to each unit's checkout. `meiva stop`, `meiva restart`, and
`meiva start` act on **one local home**; they do not stop every machine remotely.
For a planned cluster outage, coordinate the participating machines through
SSH or their existing operator entry points. Stop runners before the gateway;
start the gateway and verify its dependencies before starting runners.

## Choose the resource scope

| Command | Running agent turns | Persistent shells and schedules | Local infrastructure and extras |
| --- | --- | --- | --- |
| `meiva stop --yes --close-shell-sessions --stop-browser --stop-extras --keep-no-services --stop-infra --mode smooth --timeout 300` | Settled by the agent host's own exit | Close shell sessions with bounded known-group signals; report known job leftovers | Stop this home's services, browser, Gate, helper, native LGTM and private data plane |
| the same with `--keep-infra` instead of `--stop-infra` | Same | Close | Keep the private data plane |
| the same with `--keep-infra --keep-service gateway` | Same | Close | Also retain the named service; dependent services require `--keep-infra` |
| `meiva stop --yes --keep-shell-sessions --keep-browser --keep-extras --keep-no-services --keep-infra --mode smooth --timeout 300` | Same | Retained | Stop only the roster services; keep everything else |
| `meiva restart --all-services --keep-shell-sessions --keep-browser --keep-extras --mode smooth --timeout 300` | Same, then the start leg | Retained (`--close-shell-sessions` closes them) | Replace the native services; keep PostgreSQL, Redis, PgBouncer (always), browser, Gate, helper and native LGTM |
| `meiva restart --only agent-host --mode smooth --timeout 300` | The agent host's exit, alone | Untouched | Only agent-host is stopped; root relaunches it |
| `meiva start --all-services` | Wake every agent holding a continuation note once the host is ready | Reuse retained sessions; closed sessions are not serialized | Bring up the named services from the existing home |

Every `stop` choice is an explicit argument; a missing one exits 2 and lists every
missing argument at once. A host without gateway capability (a pure runner) takes no
`--keep-infra|--stop-infra` and refuses it if given. `restart` takes either `--only
agent-host` or a start selection plus its own keep-or-restart choices, and `start`
takes exactly one of `--all-services`, `--only-service NAME…` and `--disable-service NAME…`;
nothing about the selection is remembered between runs.

`--keep-service` is repeatable and accepts the bare service-roster name (the shared
browser, the pty-sessions service and the extras have their own choices). It is
an invocation's preservation choice, not a permanent disabled-service setting.
`stop` asks for confirmation with `--prompt` and proceeds with `--yes`; `restart` never asks. Both
require `--timeout SECONDS` (the examples use 300), and `restart`'s `--timeout` bounds its stop
leg. A deadline is a failed stop, not permission to kill surviving services. Shell sessions and Postgres are the exceptions: Postgres' fast
shutdown that has not finished by the end of its share of the budget (a hung
archive command) is ended by an immediate shutdown and the leftover descendants
are SIGKILLed, loudly and without failing the stop
([decision](../../decisions/data/database/2026-10-02-pg-stop-escalates-to-immediate.md)); for shell sessions, `stop` hangs up each shell's
known shell and signals known shell/foreground groups, escalating after a
grace of at most 10 seconds. Shell survival fails closure; known job leftovers
are diagnostic, and detached/background process disappearance is not certified. A busy
session still leaves its owner the closure notice, written to the database
while the shell sessions are closed, before the data plane stops
([decision](../../decisions/runtime/processes/sessions/2026-10-07-pty-best-effort-closure.md),
[notice write](../../decisions/agents/messages/2026-10-02-close-notices-written-at-terminals.md)). `--mode force`
explicitly selects force behavior when a normal stop cannot complete. Force stops
the selected service processes without letting the agent host write the restarts
a smooth stop leaves behind: a turn it interrupts is not resumed, and the next
host recovers only from persisted checkpoints. Force does not provide a smooth
stop's continuation or a checkpoint for interrupted arbitrary code; use a smooth
stop for the planned data-plane move.

A stop retains agent IDs, history, checkpoints, pending messages, workspaces,
browser profiles and observability data. It does not terminate agent identities
or destroy the cluster. A stop with `--close-shell-sessions` closes persistent shells; their running
processes and shell variables cannot be restored by `start`. `--keep-shell-sessions` on `stop`
or `restart` keeps them instead.
Externally launched tools are not owned by one local home.

Impersonation is a separate agent identity protocol. These commands do not
request, acquire, renew or release external-agent control leases. Also,
`meiva cluster pause NAME` is machine membership administration, not this
local stop; it stops nothing on this home.

## What a smooth stop does

A stop keeps no state of its own between commands: there is no pause to release and
no phase to resume. It clears the home's serving gate first, then stops the services
in root's declared order, one at a time, each only after the one before it has exited.

**The agent host goes first and settles the turns.** On SIGTERM it refuses new turns,
writes one `restart` per running turn, lets each turn reach it (a model call or code
run in flight is interrupted after the restart has waited its 10-second grace), and
exits; the same transaction that applies a restart queues the note that continues an
unfinished turn. Idle agents get nothing. A turn still running when the host's
budget (30 seconds) is spent is named at ERROR, cancelled, and not resumed: the step
it was in is lost, as after a crash. The next `meiva start` takes every restart and
continuation note the stop left, through the host's pending scan. How the exit is
built: [agent-host graceful exit](../../../services/agent_runner/agent_host/lifecycle/docs/graceful-exit.meiva.okf.md).

**The other services follow.** The ops server stops taking connections on SIGTERM and
lets the requests it already admitted, and the executor work they started, finish
within 15 seconds before it cancels what is left and exits. A signalled gateway drains
its in-flight connections under a finite budget
(`gateway.gateway_graceful_shutdown_timeout_seconds`, default 30s — well inside
this command's default 300s deadline); past the budget uvicorn cancels the
remaining request and stream tasks and the lifespan cleanup runs, so an
unfinished streaming response cannot hold the process open. The SSE streams do
not spend that budget: the gateway's server marks its shutdown as it begins and
each stream ends itself within one poll tick, so its client reconnects at once.
meiva-root waits for the gateway's stop longer than that budget plus the lifespan
cleanup (`ServiceSpec.stop_ceiling_s`, derived from the same setting), and for the
ops server's stop longer than its drain (`OPS_STOP_CEILING_S`), so a service still
draining is never reported as a failed stop. No remote
dispatch holds that cleanup: the TTL reaper runs as its own service, whose stop
cancels its in-flight dispatches, and rows left expired are re-selected by its
next start.

**Nothing fences the stopped home.** A gateway that is still up keeps accepting
messages while a runner is down; they stay pending in the database and the runner
works them after it starts. `meiva status` and `GET /api/cluster/status` report the
host as `paused` with `paused_reason` `startup` from the moment the stop clears the
serving gate until a start passes readiness. Services kept up with `--keep-service`
keep working.

Persistent shell sessions and the data plane stop after the services, as the table above
says. Once the root is no longer running, repeating `meiva stop` needs no cluster
configuration and finishes without fetching an offline gateway; while the root runs,
the first stop needs the actual cluster configuration.

An incomplete stop reports its failing phase, the budget each phase spent and the
surviving process inventory, and exits non-zero without killing anything that
`--mode force` was not asked to. Retry the command, or run `meiva start`.

Cold native admission consumes the saved checkpoints.
Idle agents remain idle; pending work can continue; terminated agents remain
terminated. A smooth stop preserves completed tool results. No lifecycle
command can guarantee exactly-once external effects if a process crashes after
the external effect but before its result becomes durable.

## Updating and moving the data plane

`python -m cli.fleet_update run` executes the complete update. Its `down` half
stops every unit with `meiva stop --yes` and the operator's explicit stop choices
(shell sessions, browser, extras, `--keep-service` or `--keep-no-services`, mode,
timeout; the gateway also gets its data-plane choice), and `up` starts them with the
operator's explicit service selection (the runbook's "Updating a networked cluster
in source mode"). The fleet never decides what to keep.
It judges a host's stop by two facts, both required: `meiva stop` exited 0, and the
host's `run/lifecycle-op.json` then records a stop that is complete with result rc 0.
It judges a start by `meiva start`'s exit code, which is 0 only after readiness, then
the roster and smoke checks. A host counts as already stopped (a rerun of `down`)
only when that journal's last operation is such a stop and no meiva-root is running.
The separate halves remain available for manual release work and recovery.
A stored-script upgrade uses the
[script-only maintenance operator entry](../../../schedules/README.md#script-repair-while-the-agent-host-is-stopped)
with the existing installed runtime. It refuses while an agent host holds the home's
instance lock, preserves schedule activation state, and does not waive schedule
verification.
A running schedule or PTY can retain old code and DB access outside application
root, so root exit alone is not a writer barrier: the stop gives persistent
shell sessions a bounded completed-work wait, then closes known shell sessions
best effort. This does not prove detached/background writers disappeared;
inspect residual writers before treating it as an external-work barrier. Each
busy owner receives the same closure notice as
`meiva stop`. When the operator passes `--keep-shell-sessions`, the shell sessions survive
the update and old-code shells keep running: the stop is not a writer barrier for them in
that window. `--close-shell-sessions` closes them as above; schedules are re-armed after
start.

For a move, stop all participating runners, then stop the gateway last. Check
each command's exit status before taking the final snapshots. PostgreSQL uses
smart shutdown; open clients can prevent completion. Redis uses `SHUTDOWN SAVE`
and its exact process exit is verified. Stop is not a backup: validate the
separate database/Redis snapshots and required files before restoring them on
the destination.

Home-owned native LGTM stops after its producers. Its marker, unit definitions
and data remain intact for start. Linux user units disable the supervisor's
automatic SIGKILL; a timeout remains an incomplete stop. On macOS, the stop
first waits for the observed process generation and its descendants to exit,
then removes an idle launchd job; a KeepAlive replacement observed in between
is drained too. The final launchd inspection and removal are not an atomic
admission fence. Unregistered detached jobs and OS watchdog/autostart or daily
log-maintenance producers outside the service roster need their own scope check for a coordinated machine
shutdown; do not equate local command success with every remote writer stopping.

A running agent host exits gracefully only if the code it loaded has the graceful
exit; updating source on disk does not update an imported running process. **The
first deployment of a change to that exit is not protected by it**: the host being
replaced still exits the way its loaded code does. Establish and verify the one-time
cutover procedure against the actual running version before switching to it.

## If a stop fails or is cut off

A stop keeps no journal that gates anything. What survives a CLI crash, a dropped SSH
session or a host reboot is `$MEIVA_HOME/run/lifecycle-op.json`, the status journal:

- it records the running or last stop/restart: its pid, the phase timeline and whether
  it finished, with the result (`rc`, the failing stage and the surviving processes of
  an incomplete stop). A pid that is gone without a completion means the command died
  where the timeline stops;
- it is diagnostic and gates nothing. Retry `meiva stop` with the same arguments, or
  run `meiva start`; neither needs the journal to be cleared. A write to it can fail
  without failing the stop, and it is not synced to disk, so a machine crash can lose
  its last write.

`meiva stop`, `meiva restart` and `meiva start` all run under one exclusive lock on
the home (`resource_lock` over `deploy-state.lifecycle.lock`). When another of
them holds it, `meiva start` waits up to 30 seconds and then fails naming the
holder's pid and purpose. The lock belongs to the OS and goes away with its process,
so a killed `meiva stop` never blocks `meiva start`.

A failed stop retains its process inventory. Force remains an explicit
owned-process escalation, not an inference from a timeout.

An ordinary `meiva start` brings the selected services up, waits for readiness,
marks the home serving, and the agent host's pending scan wakes every agent that
holds a continuation note. A successful service probe cannot make an unflushed
checkpoint durable; that is why a turn the stop cut short continues from its last
durable checkpoint.

A stop that fails before any service has stopped (a `restart`'s preflight, for
example) changes nothing on the host: it keeps serving, and no start is needed. A
failure after services began stopping leaves the host stopped until `meiva start`.

## Supervision and recovery ownership

Meiva root supervises the admitted application roster. Deliberate stops
remove services from that roster before closing their captured processes;
service supervision cannot reinterpret a planned stop as an unexpected crash.
The ordinary root boot unit owns replacement applications. Data-plane and persistent shell-session
custody are separate and must be reconciled explicitly.

Host startup settles its machine's `running` rows to `idling`
(`agent.ownership.hosted.settle_running_rows_at_start`; one host per machine) and
the next admission takes each agent under the new boot. Starting does not
replay arbitrary external effects.

For a drill that intentionally stops the message plane, keep its progress and
recovery access outside that plane. Record the captured operation, process
identities, current phase, intended resource scope and the actor responsible for
recovery before the stop. A surviving actor may inspect native evidence and
continue the recorded operation; it must not clear a live owner's locks or
invent a second rollout. Preserve failed-phase evidence even after successful
recovery, and verify the serving gate, selected services and native custody
before declaring the operation complete.
