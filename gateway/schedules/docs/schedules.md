# Schedule Manager & Runner

A schedule is a durable trigger and an ordinary Python payload pinned to one
machine. The host-local `schedule-manager` service runs on gateway and agent-runner
hosts. It admits `at`, `cron`, and `every` run-to-completion schedules. Waiting
consumes no process or PTY; each due slot starts one subprocess.

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

## Schedule manager responsibilities

- Partition rows by `machine`, with `machine_units` electing one active co-located
  unit so two homes do not launch the same work.
- Reconcile due slots from Postgres.
- Recover an open run by adopting its outcome or live runner identity; a process
  that disappears without an outcome settles as an error.
- Resolve per-fire secret environment names at launch rather than persisting values.
- Drain legacy `schedule_sync_requests` after observing their durable state.
- Read `latest.log` locally or through the target machine's ops RPC.

## State vocabulary

| status | meaning |
|---|---|
| `scheduled` | enabled and waiting for `next_launch_at` |
| `running` | one open run owns the current fire |
| `completed` | a one-shot exited successfully |
| `error` | the latest execution failed |
| `stopped` | disabled, eligible to be enabled later |
| `cancelled` | lifecycle cancellation; no future fire |

## Entry points

- `gateway/schedules/router.py` — HTTP mutations, lifecycle controls, logs and runs
- `meiva/schedules/__init__.py` — first-class SDK surface
- `services/wake/schedule_manager/manager.py` — host-local reconciliation
- `services/wake/schedule_manager/declarative.py` — due-slot admission and settlement
- `services/wake/schedule_manager/runner.py` — single-round process entry

The API's projections and mutation boundaries are in
[router contracts](router-contracts.md).
