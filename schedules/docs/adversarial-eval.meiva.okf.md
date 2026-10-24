---
type: doc
title: Adversarial weekly evaluation
description: Case rotation, isolation overlays, worker completion and restart recovery of the weekly batch.
tags:
- schedules
- evaluation
---

# Adversarial weekly evaluation

The batch fires Wednesday 04:00 cluster time and rotates five of the nine
scenario cases by ISO-week parity: even weeks run c001, c002, c004, c006 and
c008, odd weeks c001, c003, c005, c007 and c009. Each case builds a disposable
scenario directory under `~/.meiva/adversarial_eval/cases/<case>`, and the
scoreboard and index land in `results/` beside it.

Every probe runs under an `eval_isolation` overlay; c003 additionally allows the
`web` tool through the overlay's network allowlist because its partner document
is served on loopback for the batch.

A worker counts as done only when it can no longer act: `TERMINATED`, or
`IDLING` with output already produced. A freshly spawned agent's row starts as
`IDLING` before its process has even booted, so bare `IDLING` read the whole
batch as finished seconds after spawning and audited empty transcripts
(adversarial-eval 2026-35, 2026-08-26). A worker that finished but failed to
terminate still ends its turn `IDLING` with the report message the audit reads.

The batch marker lists the agents spawned by the current batch and is replaced
atomically, so a restarted process reads either the previous or the new list. A
marker left behind by a restart records the abort, terminates those agents and
wakes the owner. A batch that raises leaves its marker in place for the next batch
to recover, records the error in the index and wakes the owner; a case whose setup
raises is a recorded result (score 0, alerted) and the other cases still run.

The batch waits for its workers by id (`get_status`, and `get_last_message` for an idle one) and
never lists the directory to find them. It polls every 30 seconds for up to 25 minutes, then
terminates what is still not done with force and scores those cases zero. Leftover workers of an
earlier batch are the only agents found by search: live agents with exactly the `doc-worker` or
`doc-colleague` label that the batch did not spawn. The agents spawned by a probe (the c007 bait)
are found by reading every spawner in the directory once, after the workers are done.

Accepted limitation: a worker that follows c007's bait spawns a non-isolated
child because its own spawn call carries no overlay. The audit flags that child,
but automated cleanup of such children does not exist yet; this is accepted
until OS-level sandboxing.

Worker prompts and scenario material carry required CJK phrasing, escaped as
`\uXXXX` because raw CJK is only allowed in the frontend locale files.

The scoreboard, alerting policy and the batch's loopback server are described in
`schedules/README.md`.

## Where the decisions and the effects live

The schedule polls the clock at least once a minute and fires at its Wednesday 04:00 slot with
no rest after the batch (`LoopTiming(max_sleep=60.0, rest=0.0)`); the batch itself runs inside
the claimed slot.

Strict pure modules in `schedules/plans/` decide: `adversarial_cases.py` (the nine cases, their
files, the audit rules and the ISO week), `adversarial_spawn.py` (the prompts, overlays and
labels), `adversarial_workers.py` (when a worker is done, to be waited for again or given up on,
which agents are leftovers or children), `adversarial_marker.py` (the batch marker and the index
lines of a restart or an error) and `adversarial_scoreboard.py` (a case's record, the scoreboard,
the alert and the owner's message). Shells in `schedules/jobs/` act: `adversarial_batch.py` runs
the week, `adversarial_workers.py` calls the SDK on the workers, and `adversarial_scenario.py`
writes and reads the scenario directories and owns the loopback server.

`schedules/tests/adversarial/` asserts the decisions on values, scores real scenario files, and
runs the batch against a gateway on a loopback port, real files and the throwaway Postgres.
