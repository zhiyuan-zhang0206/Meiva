---
type: doc
title: Schedule loop and agent directory
description: How every built-in schedule decides when to fire and which agent to wake, and where each decision and each effect lives.
tags:
- schedules
---

# Schedule loop and agent directory

A built-in schedule script is thin. Its `__main__` block reads the world once (the schedule
id from `MEIVA_SCHEDULE_ID`, the cluster timezone, the operator's report-recipient variable,
the start time), builds the values the schedule needs and calls `run_loop`. Everything it
decides lives in a strict pure module under `schedules/plans/`; everything it does lives in
a shell under `schedules/jobs/`, which reads, calls the plan and executes its answer.

## The loop

`schedules/plans/loop_plan.py` owns the decision. `LoopTiming` is the shape of one loop and
its defaults are the daily loops:

| field | default | meaning |
|---|---|---|
| `tolerance` | 2 minutes | the next-slot search starts this far before now |
| `fire_ahead` | 0 s | a slot this close, or already due, is claimed |
| `max_sleep` | 3600 s | the longest single sleep, so a changed clock is noticed |
| `rest` | 120 s | the pause after a claim |

Cron expressions are strictly greater than their base, so a wake a fraction of a second past a
slot would otherwise jump a whole period (the 2026-08-06 miss); the tolerance is what prevents
it. `search_base(floor, now, timing)` is the later of the loop's floor and `now - tolerance`.
The floor starts at the process's start time and advances to each claimed slot, so a slot is
never searched for twice and a restart skips what it missed instead of replaying it.
`plan_step` takes each trigger's next slot and returns `Fire` (the earliest due slot, trigger
order breaking ties) or `Sleep` (until the nearest slot, capped).

The built-in loops differ only in timing: self-evolution-daily claims 60 s early
(`fire_ahead`), self-evolution-weekly 90 s, memory-steward rests 60 s, and the adversarial
weekly batch sleeps at most 60 s (`max_sleep`) and does not rest after its batch. A trigger
carries a payload that reaches the fire callback: the memory arbiter's job text, the weekly
trigger's name.

`schedules/jobs/loop.py` is the shell. `loop_once(db, schedule, fire, floor, now,
schedule_id)` computes the slots with `next_fire`, asks the plan, and claims through
`fire_slot_once` (`schedules/jobs/catchup.py`), returning the sleep, the new floor and what the
callback returned. `run_loop` is `loop_once` forever. The weekly loop keeps one extra fact,
whether its follow-up slot is armed, and carries it in `weekly_once`.

Both end in `run_rounds`, the driver that owns the clock read, the sleep, the round beat and the
failure handling. A round is a function from the instant it starts and the state the last round
returned to the state to carry and the pause to take; `run_loop` passes a floor, `run_weekly` a
`WeeklyState`. There are four resident schedule loops in this repository and only these two go
through the driver: `schedules/jobs/watch/stall_watch.py:run_watch` and
`schedules/jobs/watch/qa_keepalive.py:run_keepalive` keep their own, with a narrower policy
(catch and print, no backoff, no schedule-row write).

The claim is durable and keyed by `(schedule_id, slot)`: it commits before the callback runs,
so concurrent runners get one winner, and a crash after it loses the slot instead of retrying
it. The `schedule_id` is explicit everywhere; the entry validates it once.

## When a round fails

Until 2026-11-04 nothing protected a round: one exception in one schedule's round ended that
schedule's loop, and each of the separate schedules that ran through `run_loop` or `run_weekly`
needed a person to notice and restart it. `run_rounds` now catches `Exception` around the round
and keeps going, under a `FailurePolicy` carried by `CronSchedule.failure` (defaults for every
built-in loop):

| field | default | meaning |
|---|---|---|
| `first_delay_s` | 60 s | the pause after the first failed round |
| `max_delay_s` | 300 s | the ceiling the pause doubles up to |
| `report_after` | 3 | the failed round in a row that becomes an error |

`failure_backoff_s(failures, policy)` doubles from the first delay once per consecutive failure,
so a failure that repeats every round costs 60 s, 120 s, 240 s and then five minutes a round.
Each failed round logs a warning; the round that reaches `report_after` logs an error carrying
the traceback and writes `schedules.last_error` -- the column `meiva schedules ls` prints --
once per episode. The round that succeeds logs `recovered after N failed rounds`, clears that
column and resets the count.

Three details keep this narrower than it sounds. The floor moves only on a round that returns, so
a failure after the claim costs one extra round rather than a replay: the next round finds the
slot taken and advances past it, which is the same durable claim as before. A failed round is not
charged to the round budget, so one slow failure cannot widen every later `alive_by` and blunt
`schedule_not_progressing`; it still writes a round beat, or the backoff itself would read as a
late loop. And `except Exception` is deliberate: `KeyboardInterrupt` and the signal exits still
end the process, because that is how a supervisor stops a resident schedule.

## Agents

`schedules/plans/directory_plan.py` decides over one directory page. The SDK's directory search
matches label substrings, so only an exact label counts: `plan_directory_page` returns `Found`,
`NextPage` (follow the cursor) or `NotFound`. `schedules/jobs/agent_directory.py` walks the
pages with the SDK and acts: `ensure_agent` resurrects a terminated agent with the prompt,
messages a live one, and spawns a missing one under a fresh idempotency key.

A schedule's report recipient is a `ReportRecipient`: the agent id an operator named in the
schedule's variable, else whichever agent carries the schedule's label. The variable is
validated when the recipient is built, so a malformed value stops the schedule at its start
and never falls back to the label.

## Evidence

`schedules/tests/test_loop.py` drives both halves with chosen times against the throwaway
Postgres and runs `run_loop` in a child process; `test_agent_directory.py` runs the directory
shell against a gateway on a loopback port that serves pages and records requests. The
scripts themselves are run as `__main__` in `test_scripts_entry.py`.

The failure half is driven round by round, for `run_loop` and `run_weekly` alike: a scripted
clock and a captured `sleep` follow a round that raised into the round after it, so the pauses it
asked for are the assertion and no test waits. One test reads the heartbeat between rounds, which
is the other half of the claim -- a loop that kept running without moving its beat would be a
blind loop rather than a live one. The child-process test adds the third side: `SIGTERM` still
ends the loop, which is what separates "survives an exception" from "ignores its supervisor".
