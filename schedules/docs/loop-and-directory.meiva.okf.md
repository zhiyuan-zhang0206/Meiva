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
callback returned. `run_loop` is `loop_once` forever and owns the only sleep and the only clock
read. The weekly loop keeps one extra fact, whether its follow-up slot is armed, and carries it
in `weekly_once`.

The claim is durable and keyed by `(schedule_id, slot)`: it commits before the callback runs,
so concurrent runners get one winner, and a crash after it loses the slot instead of retrying
it. The `schedule_id` is explicit everywhere; the entry validates it once.

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
