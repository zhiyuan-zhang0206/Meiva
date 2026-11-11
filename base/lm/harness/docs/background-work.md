# Background work of an external CLI

An external agent (Claude Code, Codex, Antigravity) uses its CLI's own shell, background
tasks, timers and monitors. Meiva gives it no mechanism of its own for any of that; the
CLI's mechanisms must work under Meiva, and a mechanism that stops working because Meiva
closed the CLI is a Meiva bug
([decision](../../../../docs/decisions/agents/graph/2026-11-11-background-work-keeps-sessions.md)).

## What can be lost

The CLI process is the owner of the work. Close it and the work goes: Claude's `CronCreate`
and `ScheduleWakeup` timers live in the process (they do not survive a resume), Codex's
`unified_exec` commands die with the app-server, and an Antigravity interrupt kills `agy`.

## When a CLI process is closed

| Cause | Code |
|---|---|
| Idle TTL (`MEIVA_HOST_AGENT_IDLE_TTL_SECONDS`, 900 s) | `evict_runtimes` in `services/agent_runner/agent_host/runtime.py`, called by `AgentHost._evict` after a turn settles |
| LRU bound (`MEIVA_HOST_AGENT_CACHE_SIZE`, 32) | same function |
| Config change rebuilds the runtime | `cached_runtime` retires the old one |
| Terminate, restart, crash, cancelled turn | `AgentHost.drop_agent` |
| Host shutdown, `meiva restart`, `meiva update` | `AgentHost._close_runtimes` |
| A turn faults or the stream ends | `HarnessBridge._retire` |
| Harness switch | a new runtime is built for the new harness; the old record stays on disk |
| Antigravity interrupt | `AgySession._ensure_transport` respawns `agy` |

Every close ends in `HarnessSessions._close` → `HarnessBridge.close`. The adapter reads
its CLI's stream in its own read loop for as long as the session lives, idle or not; what
no turn of ours owns goes to the sink (`UnsolicitedOutput`) and wakes the agent as a
`system:harness` inbound.

## Keeping a session that holds work

Each session answers `background_work()` (read-only, structural protocol
`BackgroundWorkReporting` in `session/background.py`; the bridge forwards it). The host asks it
through `holds_background_work(runtime)`:

- a runtime holding work is exempt from the idle TTL;
- the size bound evicts idle runtimes first; only when nothing idle is left does it take a
  runtime holding work, least recently used first, and logs
  `harness_background_work_evicted` — that agent is told what was lost (below);
- `/stats` of the host reports `kept_alive_sessions`, the number of cached runtimes kept
  only for this reason.

No new configuration: the existing cache size is the ceiling.

## What each adapter can see

| Adapter | Seen | Blind spot |
|---|---|---|
| Claude | background commands and sub-agents (`TaskStarted` / `TaskUpdated` / `TaskNotification`); `CronCreate`, `CronDelete`, `CronList`, `ScheduleWakeup` calls and results; a turn the CLI began itself and is still running | no event or echo says a timer fired: one-shot timers end by clock (a cron's due time comes from the result, else from its own expression, read in the zone the CLI process runs in (its `TZ`, else the host's local zone, handed to the session as `cron_timezone`); an expression that cannot be read keeps the seven-day bound), or earlier when a turn begins with no echo and no task notification after they came due (that turn is classified `SCHEDULED_WAKE`); a recurring cron ends at `CronDelete` or the CLI's seven-day expiry |
| Codex | `commandExecution` items (`agent`, `unifiedExecStartup`) started and not completed, only while the app-server has a descendant it did not start with; a turn the server began itself | the process-tree check guards against an item whose `item/completed` never arrives (the app-server does send it when a background command ends); no cron-like tool exists |
| Antigravity | successful `schedule` calls without a duration (cron-like), held for one day; a timer with `DurationSeconds` blocks inside its turn, so the turn is the work | a cron-like job's firing and cancellation are not visible; background shell commands are killed with the turn. When it cannot tell, it keeps the session (bounded by the day and by the cache size) |

## Telling the agent what was lost

The session writes its pending list to its `SessionRecord` (`background`, in the
per-(agent, harness) file) whenever the set changes. After a process is gone — a host
restart, an update, a same-harness restart, a forced eviction, an Antigravity respawn, a
switch away and back — the next session reads the list back as lost
(`take_lost_background`), and `HarnessBridge._opening_input` prepends one system note to
the first input, listing each item by its description. The record is cleared when the note
is taken, so it is told once. Items whose own end time has passed (a wakeup that already
fired) are left out. The agent decides whether to recreate anything. The note is the outermost
part of the opening input: it comes before the handoff (or the compaction reminder), which comes
before the input's own text, and after a restart that text can be the graph's resume note for a
reconciled input (`reconciled-resume-…`, see `delivery.md`).

## Turns the CLI starts itself

A turn the CLI starts while the agent is idle (a fired timer) is observed as
`TurnStarted(origin=HARNESS)`. When it ends, `_wake_for_harness_turn` hands its reply to
the sink as `UnsolicitedOutput(source="harness-turn")` so the graph takes a turn over it.
Turns answering a background-task report (already delivered) or a queued input of ours (it
has its own ledger) are not reported again. Presenting such turns in the graph and the
timeline belongs to the event pump work, not to this file.

## Records, migration and transfer

`SessionRecord.background` is a required field. A record written before it existed is refused on
read (the same way an old single-slot record is), and `meiva converge` backfills it as an empty
list (`backfill_record_fields`, run by the harness session records step) before the runner
starts, together with `generation`; `meiva harness verify-sessions` and `unreadable_records` report a record
missing either field as unconverged. An empty list is the true value for a record written by a process
that tracked nothing.
When an agent moves to another machine, the record travels with its pending list as it stood. The
CLI process stays behind, so on the target the list is lost work: the target's first session reads
it back and the agent is told once.
