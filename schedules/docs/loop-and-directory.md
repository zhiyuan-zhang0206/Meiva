# Declarative rounds, compatibility loops, and the agent directory

Built-in schedule entries are single-round programs. The host-local schedule
manager owns cadence, admission, timeout, run history, and overlap prevention;
an entry reads `MEIVA_SCHEDULED_FOR`, performs one unit of work, and exits.
Decisions live under `schedules/plans/`, while effects live under
`schedules/jobs/`.

## Declarative rounds

`schedules/manifest.json` declares each built-in's `cron` or `every` trigger,
cluster-relative timezone, and timeout. When a slot is due, the manager:

1. atomically opens one `schedule_runs` row, refusing a second open run;
2. materializes `run.py` in the schedule workspace and launches it without a
   PTY;
3. exports `MEIVA_SCHEDULE_ID`, `MEIVA_SCHEDULED_FOR`, and the owner id when
   present; and
4. settles the run and advances recurring schedules from `max(slot, now)`.

The durable slot, rather than an ambient clock reading, distinguishes 04:00
from 07:00, Tuesday from Friday, or a watch round from its five-minute liveness
check. Manual `--once` modes may use the current time because they are not
replaying a managed slot.

## Workspace state

System schedules run in `$MEIVA_HOME/schedules/<id>/`; agent-owned schedules run
in `$MEIVA_HOME/workspaces/<agent>/schedules/<id>/`. A job that carries state
between rounds writes ordinary files in that directory. The shipped examples
use `state.json` for:

- the database-stall three-sample window and alert cooldown;
- QA keepalive failure streaks;
- main-red and agent-watch observations;
- the self-evolution Tuesday-to-Friday follow-up arm; and
- `meiva_usage` provider-rate-limit pacing.

Entries that previously stored state under `$MEIVA_HOME/run/` copy that file
into the workspace on their first declarative round. The source file stays in
place as rollback evidence.

## Compatibility loops

`schedules/jobs/loop.py`, `run_weekly`, `run_watch`, and `run_keepalive` remain
for stored `trigger_kind='daemon'` rows created before declarative triggers.
New built-ins do not call them. Provisioning upgrades an existing built-in or
plugin row in place, preserving its `enabled` value; a pending sync request
blocks declarative admission until the manager has reaped the old
`meiva-schedule-<id>` PTY.

The compatibility decision layer remains in `schedules/plans/loop_plan.py`, and
its `(schedule_id, slot)` claim still prevents duplicate fires for rows that
have not migrated.

## Agents

`schedules/plans/directory_plan.py` decides over one directory page. SDK label
search is substring-based, so only an exact label counts:
`plan_directory_page` returns `Found`, `NextPage`, or `NotFound`.
`schedules/jobs/agent_directory.py` follows the pages and acts:
`ensure_agent` resurrects a terminated agent with the prompt, messages a live
one, and spawns a missing one under a fresh idempotency key.

A report recipient is a `ReportRecipient`: an operator-supplied agent id, or the
agent carrying the schedule's label. Invalid operator input fails before work
starts. A declarative entry binds `schedule:<id>` as its actor, and an
agent-owned schedule carries the owner id without gaining authority to restart
or terminate that agent.

## Evidence

`schedules/tests/test_scripts_entry.py` proves every manifest entry imports from
a materialized copy and contains no resident driver. Watch tests prove state
survives separate rounds. `services/wake/schedule_manager/tests/` covers
single-flight admission, timeout custody, scheduled-slot export, workspace
locality, and the daemon-to-declarative PTY handoff. Compatibility loop behavior
remains covered by `schedules/tests/test_loop.py`.
