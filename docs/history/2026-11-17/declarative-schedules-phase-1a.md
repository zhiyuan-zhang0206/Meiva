# Declarative schedules, Phase 1A

## Decision

Schedules with `at`, `cron`, and `every` triggers are durable timing declarations whose
payload is an ordinary run-to-completion Python program. The schedule manager keeps no
per-schedule process while waiting. At a due slot it starts one non-PTY process, records one
run, and advances durable timing state after that process exits.

The existing resident schedule model remains temporarily available as the `daemon` trigger.
Existing rows migrate to that trigger so the execution-model change can be deployed before
the installed payloads are converted in later phases.

## Why

Resident sessions made the waiting mechanism and the work mechanism the same object. That
spent PTYs while idle, made a clean process exit ambiguous, and put state in a session rather
than in the schedule row and its workspace. A durable next-fire timestamp plus a bounded
single-round process gives recovery one answer: claim the due slot once, observe its outcome,
then compute the next slot.

One-shot schedules catch up once after downtime. Recurring schedules advance from the later
of their claimed slot and the current time, so recovery does not replay a backlog. An open run
is the single-flight claim, preventing overlapping executions.

An in-flight process stays bound to its launching manager and workspace. Machine moves and
deletion wait for the open run to close; this keeps recovery based on one durable run record
instead of transferring or orphaning an already-started OS process.

## Workspace and ownership

An agent-owned schedule runs from that agent's workspace and defaults to the agent's machine.
A system schedule runs from the cluster schedule workspace. The working directory is ordinary
storage for `run.py`, `latest.log`, and payload-owned files such as `state.json`; the runtime
does not invent a state API.

Owner termination cancels unfinished schedules whose lifecycle policy is `cancel`. `keep`
makes the schedule independent of the owner's lifecycle. The owner affects placement,
workspace locality, and attribution; it does not grant additional authority.

## Alternatives rejected

- A resident sleep loop per schedule retained the idle process and PTY costs and preserved
  session state as a second source of truth.
- A framework-specific `ctx.state` or completion callback made payloads depend on a scheduler
  runtime. Ordinary Python files and exit status already provide the required contract.
- Replaying every missed recurring slot after downtime could create an unbounded launch storm.
- Converting every installed resident payload in the schema rollout would couple compatibility
  migration to the new engine and remove the zero-downtime deployment path.

## Follow-up

Later migration phases convert the compatibility `daemon` rows and retire their resident
session path after each installed payload has a run-to-completion equivalent.

## Update: restart-safe process custody

The earlier statement that an in-flight process stays bound to its launching manager is
superseded. A run stays bound to its machine and workspace, while its OS custody survives a
manager restart. Before `run.py` can execute, the manager records a run-specific claim with the
dedicated process-group leader's native birth, PGID, and deadline, then releases a one-byte
launch gate. Loss of the manager before that release closes the gate without starting the
payload; loss after release leaves enough durable evidence for the replacement manager to adopt
or stop the exact group.

The payload shares the supervisor's dedicated process group. A supervisor killed without an
outcome therefore does not release single-flight: reconciliation first kills any surviving
group members and proves the group empty, then settles the run. Timeout and cancellation use
the same order. An unreadable process identity or group probe keeps the run open because an
unknown ownership result cannot certify absence.

## Update: generation-bound payload custody

The durable claim now records the payload's native birth before a second launch gate lets
`run.py` execute. This member identity preserves custody when the supervisor is gone. Before
signalling the numeric process group, reconciliation stops one exact recorded birth, verifies
that it still belongs to the claimed group, and then sends the group kill. The stopped member
keeps the PGID reserved across that final check, preventing a later process generation from
receiving the signal.

When every recorded birth is gone but the same numeric PGID exists, custody remains unknown:
the group may belong to a later generation observed after an outage. Reconciliation neither
kills that group nor closes the run. This conservative state preserves single-flight until
ownership or absence becomes provable.
