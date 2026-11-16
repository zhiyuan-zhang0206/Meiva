# Schedule Manager & Runner

A schedule is a durable trigger and an ordinary Python payload pinned to one
machine. The host-local `schedule-manager` service runs on gateway and agent-runner
hosts and owns two execution models during the migration:

- `at`, `cron`, and `every` are declarative, run-to-completion schedules. Waiting
  consumes no process or PTY. A due slot starts one no-PTY subprocess.
- `daemon` is the compatibility model for installed resident schedules. Its script
  owns its timing loop and runs in a supervised PTY session.

New public creation surfaces admit only declarative triggers. Existing rows were
backfilled as `daemon`, allowing payload conversion to happen separately from the
engine rollout.

## Declarative execution

The schedule row stores the next eligible UTC instant. Reconcile claims a due row
and inserts its `schedule_runs` row in one transaction; the open row is the
single-flight guard. The process runs with stdin closed, output redirected to
`latest.log`, a new process session, and a finite timeout. Its exit outcome closes
the same run row.

An `at` row catches up once when its instant passed during downtime. Success becomes
`completed`; failure becomes `error`; both disable the row. Recurring rows compute
their next fire after the later of the claimed slot and current time, avoiding both
overlap and replay storms after downtime.

The execution directory is a locality contract:

- agent-owned: `$MEIVA_HOME/workspaces/<agent>/schedules/<schedule>/`
- system-level: `$MEIVA_HOME/schedules/<schedule>/`

`run.py` and `latest.log` live there. Payload state is an ordinary file in the same
working directory; there is no scheduler-specific state or completion API.

`owner_agent_id` also defaults placement to that agent's current machine and stamps
run attribution. An owner lifecycle policy of `cancel` disables unfinished work when
the agent terminates; `keep` leaves it independent. The owner changes attribution and
locality, not authority. Details are in
[schedule identity](../../../services/wake/schedule_manager/docs/schedule-identity.md).
Machine moves and deletion are accepted once an open run closes, so an in-flight
process cannot be detached from the manager and workspace that launched it.

## Compatibility daemon execution

Daemon rows retain revision convergence, the named `meiva-schedule-<id>` PTY, runner
liveness, and explicit start/stop/restart behavior. Their request receipts and
recovery rules remain in [schedule convergence](schedule-convergence.md), liveness in
[schedule liveness](schedule-liveness.md), and payload process behavior in
[schedule runner](schedule-runner.md).

The built-in slot-claim helper remains inside compatibility payloads while they are
resident. It is documented in [cron slot claims](cron-slot-claims.md).

## Schedule manager responsibilities

- Partition rows by `machine`, with `machine_units` electing one active co-located
  unit so two homes do not launch the same work.
- Reconcile declarative due slots from Postgres before converging daemon sessions.
- Recover an open declarative run by adopting its outcome or live runner identity;
  a process that disappears without an outcome settles as an error.
- Resolve per-fire secret environment names at launch rather than persisting values.
- Consume daemon `schedule_sync_requests`; declarative lifecycle mutations update
  durable timing state directly and need no PTY convergence request.
- Read declarative `latest.log` locally or through the target machine's ops RPC;
  daemon logs retain live-capture and saved-transcript fallbacks.

## State vocabulary

| status | declarative meaning | daemon compatibility meaning |
|---|---|---|
| `scheduled` | enabled and waiting for `next_launch_at` | unused |
| `running` | one open run owns the current fire | manager's recorded resident launch |
| `completed` | a one-shot exited successfully | resident script exited successfully |
| `error` | the latest execution failed | launch or resident execution failed |
| `stopped` | disabled, eligible to be enabled later | disabled and session reaped |
| `cancelled` | lifecycle cancellation; no future fire | lifecycle cancellation; session reaped |

## Entry points

- `gateway/schedules/router.py` — HTTP mutations, lifecycle controls, logs and runs
- `meiva/schedules/__init__.py` — first-class SDK surface
- `services/wake/schedule_manager/manager.py` — host-local reconciliation
- `services/wake/schedule_manager/declarative.py` — due-slot admission and settlement
- `services/wake/schedule_manager/runner.py` — declarative and daemon process entry

The API's projections and mutation boundaries are in
[router contracts](router-contracts.md).
