# Delete the maintenance hold

## Context

`meiva stop` and `meiva restart` took a home-local hold (`run/deploy-pause-owner.json`,
`base/deploy/maintenance/`, `ops/agent_pause/`) and walked it through five phases before
and while services stopped. The hold carried one guarantee that mattered: every running
turn stopped at its claim boundary and left a restart in Postgres for the next host to
wake. The rest of it existed so that a command outside the host could prove that: the
journal, per-agent receipts, a cohort frozen at preparation, admission gates on the host,
the schedule manager and the ops server, a 503 on every business route, the quiesced
window and the `host_deploy_state` posture it wrote.

Two earlier decisions removed the reason for the proof. A SIGTERMed agent-host now writes
a restart for each running turn itself and drains it within a bound
([the host's graceful exit](2026-10-20-host-graceful-exit-writes-own-restart.md)),
and a restart interrupts what its turn is doing after a grace and resumes an unfinished
turn from a queued note
([the restart that interrupts and resumes](2026-10-20-restart-interrupts-after-a-grace-and-resumes-by-a-note.md)).
With the fencing and the pointer gone, the hold and the host's exit ran the same
restart through the same code; the hold's drain had nothing left to do that the exit
did not.

## Decision

- Delete `base/deploy/maintenance/` and `ops/agent_pause/`, the admission and held-control
  branches of the host and the hosted admission, the schedule manager's and the ops
  server's hold gates, the gateway's `business_paused` middleware and its route-contract
  `PauseSemantics`, the `maintenance-hold` diagnostic with its event and alert, the
  failed-continuation event and alert, the lifecycle-wait event and setting, and the CLI's
  phase machine, `release_self_heal_pause`, `resume_after_start` and `hold_report`
  (`meiva status --json`, whose only payload it was).
- A stop clears the home's serving gate, then stops the services in root's declared
  order: the agent-host first, awaited, so its own SIGTERM exit settles the running turns;
  the other services follow. Nothing fences the home afterwards; a message sent while a
  runner is stopped stays pending and is worked after the start. `meiva status` reads
  `paused` with reason `startup` from the stop until a start passes readiness.
- The ops server drains the requests it has admitted and their executor work for
  `OPS_DRAIN_S` (15 s) after SIGTERM, on its own path, with its own stop ceiling; the
  stop no longer waits for ops from the CLI side.
- The fleet update judges a stop by `meiva stop`'s exit code and the host's
  `run/lifecycle-op.json` (a completed stop with rc 0), and a start by `meiva start`'s exit
  code. A host is already stopped when that journal's last operation is such a stop and no
  meiva-root runs. `unit_already_stopped` reads the same machine fact.
- Rewriting a stored schedule script requires that no agent host holds the home's instance
  lock; this is the one guard kept, because the write is irreversible.
- Alert rules whose events only the hold raised are retired with tombstones.

## Failures that no longer exist

| Failure the hold guarded against | Why it is gone |
|---|---|
| A running turn's continuation lost on stop | The host's exit writes the restart; a turn past the 30 s budget is named at ERROR and not resumed, as after a crash. |
| A stop left a hold nobody released (a forgotten `deploy-pause-owner.json`, a stranded hold a person had to find) | There is no hold. The only state a stop leaves is `run/lifecycle-op.json`, which gates nothing. |
| A resume released by the wrong generation | There is no resume. |
| Business requests reaching a half-stopped home | A stopped runner takes none; a gateway that is up queues the messages. |
| PgBouncer waiting on pools of runners that stayed alive (2026-09-12) | Unchanged from the [loop-gating decision](../updates/execution/orchestration/2026-10-20-stop-window-loop-gating-removed.md): SIGINT to the pooler, and services stop before the data plane. |

## Consequences

- A turn that outlives the host's drain is cancelled and not resumed, where a hold-driven
  stop would have failed and waited for an operator. The agent's restart row stays for the
  next host.
- `meiva status --json` and the `hold` field of the fleet probe no longer exist; so does the
  operator's manual `rm run/deploy-pause-owner.json`. A file a previous release left behind
  is ignored.

## Alternatives rejected

- Keep the hold as a second drain beside the host's exit: both write the same restart; the
  second adds the journal, the receipts and the gates for no guarantee.
- Fence the gateway while a runner is down: the messages are durable already, and a fence
  is a second source of "paused" to keep in step with the first.

Forward link: this deletion completes what
[the host's graceful exit](2026-10-20-host-graceful-exit-writes-own-restart.md) left for
later ("the hold stays for now").
