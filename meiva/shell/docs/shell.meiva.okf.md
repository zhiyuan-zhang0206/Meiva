---
type: doc
title: meiva.shell — Shell Operations
description: "Interface for executing shell commands. Three modes: one-shot run(), background run_background() (auto-report on completion), persistent sessions."
tags: []
---

# meiva.shell — Shell Operations

## What it is

Interface for executing shell commands. Three modes: one-shot `run()`, background `run_background()`, persistent sessions `sessions`.

## Core API

### One-shot
- `run(cmd, *, cwd=None, timeout=30.0) → ShellResult` — Run command, return stdout as a string with the exit status attached: read-only `.returncode` (0 = success, non-zero = failure) and `.stderr`. String operations on the result return a plain `str` without these fields. Non-zero exit does not raise an exception; after `timeout` seconds kill the command and raise `subprocess.TimeoutExpired`. Default working directory is agent's workspace.

### Background (auto-report on completion)
- `run_background(cmd, *, name, cwd=None, keep=False, ttl, notify=None) → SessionHandle` — Run a long command in a new persistent session, return immediately, then the session auto-closes. `name` is a lowercase slug; `page-` names are reserved for pages. Omitted `notify` applies the target agent's `completion_notice_policy`: `all` (the default) preserves one completion message per exit and `hourly` emits a restart-safe hourly digest of every completion. Explicit `notify="always"` overrides that agent policy for this run. The message carries only the log path and the last 3 output lines; the log's last line is `[exit N]`, so the tail shows the command's own status (the message text names none). Output is streamed to `.shell_logs/<sid>_<name>.log` in workspace (relative path, can read/grep to view progress) and visible live in the session capture. `keep=True` retains the session after command ends. `ttl` max 86400s (24 hours). Use for one-shot long tasks like build/test/download; interactive programs still use `sessions`. Returns `SessionHandle(session_id, output_path)` — the same handle the watcher entries return. `sessions` accepts the handle itself or its `session_id`.

### Persistent Sessions (`meiva.shell.sessions`)
- `new(name: str, *, ttl) → int` — Create a named session (name is a lowercase slug; `page-` names are reserved for pages), return session ID. `ttl` (seconds) is **required** (max 86400 = 24 hours — sessions live at most one day): the gateway force-kills the session when it elapses, so pass a large value for a long-resident session.
- `send(id, cmd, *, enter=True)` — Send command to session. Asynchronous—returns without waiting for command completion. `enter=False` only types without submitting.
- `send_keys(id, *keys)` — Send raw keystrokes (e.g., `C-c`, `Escape`, `Up`, `Enter`).
- `capture(id, lines=200, *, scrollback=True) → str` — Read the last N lines of output. `scrollback=False` only captures the currently visible screen (for full-screen TUI programs, `lines` is ignored).
- `kill(id)` — Terminate session.
- `list() → dict[int, str | None]` — List your sessions (id → name, unnamed as None).

## Key Dependencies
- [[agent/docs/sessions.meiva.okf.md]] — the session backend is the underlying implementation of sessions
- [[meiva/shell/coding_tools/docs/coding-tools.meiva.okf.md|meiva.shell.coding_tools]] — launchers that start Claude Code / Codex in these sessions (not agent-facing)

## Notes
Sessions retain cwd, environment variables, and background processes, used to drive interactive CLI tools like Claude Code, Codex. Sessions outside the spawning checkout, including paths under its `.worktrees/` or `.claude/worktrees/` sibling-worktree directories, receive the venv-prefixed PATH but omit `VIRTUAL_ENV`, preventing a bare uv command from selecting the spawning checkout's venv. The shell is a login shell (`bash -l -i`), so the user's profile may still reorder PATH; the home rides `MEIVA_HOME` (else `~/.meiva`), which a bare `meiva` (the host's link to the production CLI) reads. Sessions survive after the agent process exits (not reclaimed with the process) and across agent, agent-host, and gateway restarts — the machine's `pty-sessions` service holds them, so only its own `kill`, a terminate of its agent with `kill_all_shell_sessions` (silent), its shell exiting, its TTL expiring, `meiva stop` or `meiva restart` (which close shell sessions; so do updates), a restart or crash of that service, or a machine reboot ends it; watchers are also special sessions and appear in `sessions.list()`. TTL reclamation notifies the owner only when it interrupts a running job; an empty shell's reaping is silent.

The SDK, including Codex/Claude launchers and watchers, supplies
`forward_env_dict(activate_venv=...)`. `PtySessionBackend.new_session` builds
the same cwd-based projection when `env` is omitted; an explicit empty dict
stays empty. The PTY shell fork clears inherited `VIRTUAL_ENV` before applying
the projection, so a launcher or permissions-helper host cannot restore an
omitted activation. Other ambient variables remain inherited; explicit
envfile activation and watcher runner overrides still win. This is a
creation-time rule; a later `cd` does not change it.

Page servers opened with `meiva.ui.serve` run in your own sessions (`page-<name>`, one entry per open page; the label grammar is owned by `base/sessions/page_session.py`) and appear in `sessions.list()`; see the [[meiva/docs/ui.meiva.okf.md]] server lifecycle for closure through `close()` or TTL expiry — killing the entry does not close the page, and a terminate's `kill_all_shell_sessions` spares them.

A background command's completion notice fires when the command subshell exits —
except that a closing run (`keep=False`) first waits, bounded at one second, for
the run's own `[exit N]` line to reach the end of its log. The session close kills
the process group the moment the shell exits, so a `tee` caught mid-drain died
with the command's last lines still in the pipe, leaving a log file that existed
and stayed empty; that line is the proof the log is complete, and the shell does
not exit before it lands. The wait is on the file rather than on the writer: a
process substitution is not a waitable child in bash 3.2, which macOS still
ships. The bound only bites when a grandchild inherits stdout and outlives the
command. A retained session (`keep=True`) never closes on the command, so it
fires at the subshell exit and a grandchild must not postpone it; the writer keeps
draining into the log and session capture in the background. The log's last line is `[exit N]`,
written before the pipe closes, so the notice's tail — and any later read of the
log — carries the command's own status; the message text still names no status. The launch line is sourced from disk
to keep long paths out of a canonical-mode PTY input buffer, and it preserves
the command status, keep/exit behavior and the watcher's orphan-guard parent.
