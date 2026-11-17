# meiva.watcher — what happens when a watcher's session ends

A watcher is a shell session ([meiva.watcher — Background Listener](watcher.md)), so
its end is its session's end. Nothing below ever re-spawns it.

- **The watcher exits on its own** (script finished, one-shot fired, timeout watchdog) — the shell layer sends the usual completion notice (log path, last 3 output lines), respecting the `notify` policy, and the session closes itself. Nothing else to do; re-launch it yourself if you still need it.
- **You kill it yourself** (`meiva.shell.sessions.kill` / `kill_all`) — no extra message: the call already returned its result to you in the same turn.
- **Its owner reaches `terminated`** — the lifecycle close immediately ends every persistent shell owned by that agent, including watcher sessions. The watcher cannot fire or deliver a later `watcher:<id>` wake. Use `meiva.schedules` with `on_owner_terminate="keep"` when a future trigger must survive owner termination.
- **The platform reclaims it at its TTL deadline** (`meiva.shell.sessions`' TTL reaper — a watcher's shell TTL IS its target deadline: launch = created + timeout, cron = end, at = moment + grace) — you get the ordinary shell-reclaimed interruption notice, because a watcher always has a running job when this fires.
- **A `meiva stop` or `meiva restart` that names `--close-shell-sessions` (updates included) force-closes busy sessions** — you get the closure notice for a busy session, written by the stop itself (`cli/commands/lifecycle/stop/shell_sessions.py`), the same as any other shell with work in flight.
- **`meiva stop --mode force` (`force_close_shell_sessions`, same module) or the pty-sessions service ending with no orchestrated closer** (an external SIGKILL, a machine power loss, a service crash with nothing to record it) — accepted gap: you get no notice in any of these. The session is simply gone from `meiva.shell.sessions.list()` on your next check. Nothing is watching for these cases, by design (see the decision record for why); if a watcher's absence would matter, check for it explicitly rather than relying on a message.

None of these ever re-spawns the watcher. Decide whether to re-create it yourself.

**Orphan governance (task #1726).** A watcher whose session ended without taking it along (a pty-sessions service crash, an external SIGKILL of the shell) is reparented to init with its session gone — still alive, still firing cron/at; 49 of 85 watcher processes on the fleet host were once such multi-generation orphans. The generated bootstrap arms an **orphan guard**: a daemon thread comparing `os.getppid()` against the boot-time parent every 5s, hard-exiting with code 125 on a mismatch, so session end → child death within seconds on every such path. This is the only thing standing between a dead session and a watcher that keeps firing forever — nothing external scans for or kills orphaned watcher processes any more.

Parent: [meiva.watcher — Background Listener](watcher.md).
