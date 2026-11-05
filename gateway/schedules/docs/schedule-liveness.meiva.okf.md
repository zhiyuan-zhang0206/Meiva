---
type: doc
title: Schedule liveness
description: Two questions with two records — whether a session exists, and whether the runner that claimed the schedule is still that process.
tags: [gateway]
---

# Schedule liveness

A schedule's session is an interactive login shell running the runner command, and the
shell outliving its runner is normal: a runner killed with SIGKILL leaves the shell and
its session record in place. "A session exists" is therefore not "the schedule is
running", and the two are answered by two different records.

## Which process claimed the schedule

The runner writes `run/schedules/<id>.runner.json` when it starts: its pid and the native
birth the kernel reports for it, captured by the process itself. It removes the record
when it exits on purpose. A record therefore exists only for a process that did not get
to say goodbye, and the manager asks the process table instead of a timestamp:

- **alive** — that pid still is the recorded process;
- **dead** — the pid is gone, is a zombie, or now belongs to a different process (a
  reused pid never inherits a claim);
- **unknown** — no record, an unreadable one, or a process table that cannot be read.

Only *dead* is acted on, and it takes the path a lost session takes: the schedule is
recorded as an error with `schedule_process_lost`, and with nothing restarting it
`schedule_stalled` follows two hours later. *Unknown* is said once per set of schedules
and acted on by nothing — "I could not read it" must never authorize the recovery a death
does. Sessions started before runners recorded themselves stay unknown; a relaunch gives
them a record.

## Which schedules are still iterating

The record says a process lives, not that it is doing anything. For that, a resident
schedule writes `state/schedules/<id>.heartbeat.json` each round, naming when it expects
to be back; a passed deadline raises `schedule_not_progressing` once per episode.
Schedules without a heartbeat — and every schedule between two of its own rounds — are
unaffected. A schedule whose runner is proved dead is *not* also reported here: one
outage keeps one event per stage.

## Relationship to Other Nodes

- [[gateway/schedules/docs/schedules.meiva.okf.md|Schedule Manager & Runner]] — the supervisor these liveness readings belong to.
- [[gateway/schedules/docs/schedule-runner.meiva.okf.md|Schedule runner]] — the process that writes the record.
