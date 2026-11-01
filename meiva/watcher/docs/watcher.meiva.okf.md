---
type: doc
title: meiva.watcher — Background Listener
description: '`meiva.watcher` starts background processes to wake you when specific conditions trigger—avoid polling. Three modes: time-based (`at`/`cron`), custom code (`launch`).'
tags:
- agent-view
- sdk
- agent-lifecycle
---

# meiva.watcher — Background Listener

## What it is

`meiva.watcher` starts background processes to wake you when specific conditions trigger—avoid polling. Three modes: time-based (`at`/`cron`), custom code (`launch`).

A watcher is nothing more than a shell session running a generated script —
there is no separate registry, desired-state record, or boot reconcile
(docs/decisions/runtime/updates/recovery/2026-09-27-watchers-are-never-restarted.md). Everything that
applies to an ordinary `meiva.shell.sessions` session applies to a watcher's
session too: list it, capture its output, renew its TTL deadline, or kill it.
Nothing ever restarts a watcher automatically — write watcher scripts
assuming they may be cut off at any moment and will not be re-run for you.

## Core API

- `at(when, message, *, name, notify=None) → int` — One-time scheduled wake-up. `when` supports TZ-aware datetime / timedelta / ISO-8601 strings. Returns session ID (can be cancelled with kill).
- `cron(expr, message, *, timezone=None, end_time=None, name, notify=None) → int` — Periodic wake-up via 5-field cron expression; returns session ID. Expiry and repeat-call behavior: [[meiva/watcher/docs/timing.meiva.okf.md]].
- `launch(code, timeout, *, name, notify=None) → int` — Run custom Python code as watcher. Inside the code, call `meiva.agents.send_message(meiva.self.AGENT_ID, content)` to wake you. Force-stop after `timeout` (exit code 124).

Omitted `notify` marks the shell-level send as a plain platform completion (`meiva/shell/background_plan.py:notified_line`); the gateway resolves it against the target agent's `completion_notice_policy` (`all` default, or `hourly`) at delivery time, not at spawn — `hourly` retains every completion in its digest. Explicit `notify="always"` is baked into the shell command at spawn and overrides the policy for that one run.

## What happens when a watcher's session ends

Self-exit, your own kill, TTL reclaim, a stop that force-closes busy sessions, the
accepted no-notice gaps, what happens to a watcher that outlives its owner, and the
orphan guard that kills one whose session is gone:
[[meiva/watcher/docs/lifecycle.meiva.okf.md]]. None of them ever re-spawns a watcher.

## Key Dependencies
- [[shell.meiva.okf.md]] — a watcher's underlying session IS an `meiva.shell.sessions` session: `meiva/watcher/spawn.py:run` creates it on the agent's `ShellSessions` and uses `meiva/shell/background.py` for the log and launch file
- `base/host/env/dotenv_boot.py:project_watcher_runner_env` (called with the process environment and homes by `meiva/watcher/wiring.py`) validates an agent-profile launcher's runner-class DB login (with its `MEIVA_DB_GENERATION` marker when delivered) and runner Redis URLs before session allocation and passes them only to the watcher session. **That projection is an override, not the token's carrier**: the machine token reaches a watcher from the pty session service's own environment — the daemon is started with the token its host issues (task #5979) — so a launcher with no projection at all (a seat rebuilt from the launchd side) still yields a watcher that can send. A secured default-home gateway refuses a launcher without that projection before creating a watcher; other homes and pure agent-runner units retain their child config source. The data-plane owner-URL guard remains the child-side boundary.

## Notes
Generated watcher scripts (`watcher_<session_id>.py` + `_boot.py` + `_launch.sh`) land under `$TMPDIR/meiva/<cluster>/<agent>/watchers/` (`meiva/watcher/files.py:watchers_dir`) — temp storage because they are read exactly once at launch; the per-cluster + per-agent path segments keep co-located clusters (each with its own DB-assigned session ids, which may overlap) from colliding. The generated script files only need to survive launch — there is no other persistence, and liveness is the session itself. While running, a watcher is a normal shell session—visible in `meiva.shell.sessions.list()`. Timeout parameter supports seconds, `timedelta`, or `"<n>{s,m,h,d}"` strings. When a watcher stops (self-exit / crash / timeout), the shell layer sends the policy-selected completion notification (exit code + log path of full output + output tail), then the session auto-closes—even if the child process gets SIGKILL, an eligible notification is not lost. The dedicated `remind` primitive has been removed: waking yourself = sending yourself a message (generic `meiva.agents.send_message`); at/cron generated scripts internally use the same delivery path (source marked `watcher:N`).

Generated-file tests replace send without executing the scripts. A live
session's files must survive sibling launches because asynchronous shell startup
may not have read them yet; only dead-session files are eligible for pruning.
These tests establish file retention, not successful watcher execution.

`launch`, `at` and `cron` return the same `SessionHandle(session_id, output_path)` as `run_background`; `sessions` accepts the handle or its id.

Relative deadlines and session TTL share one captured snapshot; see [[meiva/watcher/docs/timing.meiva.okf.md]].
