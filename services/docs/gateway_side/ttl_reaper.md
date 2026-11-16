# TTL Reaper — wall-clock deadline enforcement

## What is it
The service that enforces every deadline the cluster hands out. It runs once per cluster on the gateway side (`ServiceSpec.capabilities=GATEWAY`, health port slot `ttl_reaper` = 8121), as its own root unit: stopping or restarting it never touches the HTTP gateway, and the gateway holds no loop that can kill a shell.

## Loops
Two resident sequential loops under one `TaskGroup` (`services/upkeep/ttl_reaper/daemon.py`), each running a round every `MEIVA_TTL_REAPER_POLL_INTERVAL_SECONDS` (default 60s). A loop that raises cancels its sibling and ends the process; the supervisor restarts it. An unreachable database skips a round. Each loop reports its own progress to `/healthz`.

- **`sweep`** (`sweep.py`) — database only: expired `web_sessions`, impersonation leases (reminded, then reaped), plus three slow phases claimed in `maintenance_state` (`cadence.py`): the `schedule_fire_log` retention prune (daily), the torn lifecycle-pointer scan and the absent-machine fence settle (hourly, `lifecycle_fences.py`).
- **`remote`** (`remote.py`) — calls out in three phases per round: persistent shell sessions whose owner is already `terminated` are closed on their home machines, TTL-expired sessions are reclaimed, then a stale sweep drops rows for sessions their home machine no longer lists. Machines run concurrently, rows of one machine stay ordered, and each dispatch uses the RPC-budget deadline. The stale sweep performs one `shell_probe` per (machine, agent) and one bounded batch per round; a row becomes eligible five minutes after creation. Which rows go to which machine, how a runner's answer settles a row and what the owner is told are decided by the strict `shell_plan.py` and `owner_text.py` from values; the daemon reads `TtlReaperConfig` (poll interval, fire-log cadence and retention, shell-kill deadline, notice timezone) once from the boot owner and hands it to every loop.

## Semantics that matter
- A shell row is deleted only on a definitive verdict (`killed` / `absent` / `machine_absent`); runner results use `ShellKillMode` from `base/api_contracts/agent_contract.py` (`killed` / `absent`), while `machine_absent` requires local registry evidence and cannot come from a runner payload. Unknown or missing runner modes are logged and deferred; an unreachable machine, a failed op or a dispatch past its deadline leaves it for the next round. Each kill re-checks the row is still expired against `clock_timestamp()`, which pairs with the renewal guard so a renewed session is never killed.
- A terminal owner is a first-class reclamation signal: each remote round dispatches `shell_kill` for its rows and deletes an exact matching row only after a definitive verdict. This catches direct database termination and an unavailable home-host cleanup path. The deadline is not the only way a shell row becomes garbage either: a session that dies with its host leaves its row until reconciliation. The stale sweep settles those — one `shell_probe` per (machine, agent) lists the host's live sessions and the rows of the sessions it did not list are dropped — but only on a definitive answer: an unreachable host, a failed op, a dispatch past its deadline or a malformed listing leaves every row alone, because dropping a live session's row leaves that session unreclaimable forever (nothing could renew or reclaim it again, and the unexpired-row count a duty's health check reads drops with it). A row is exempt for its first five minutes, and the delete names the row's own `expires_at` and `created_at` so a row renewed or rewritten since the select survives. The sweep sends no owner notice: the session it described is already gone. Each sweep ends with one summary line — `stale sweep: candidates= pairs= dropped= deferred= machine_absent=` — and each dropped row emits its own identity (`agent_id`, `session_id`, `machine`, `created_at`, `expires_at`, `evidence`), since the row itself is gone.
- A machine missing from the machines registry is not by itself a dead machine: the registry is a table, and a rebuild, a restore, a rename or one bad bulk update makes a live host disappear from it for a while. Its absence terminalizes the machine's candidate rows only together with silence — nothing anywhere has recorded a probe of that machine (`machine_probe.last_probe_at`) inside the same five-minute window — and it is logged once per machine as `machine_absent machine= rows= agents= last_probe=`. A machine probed recently is a registry gap: its rows wait and the sweep logs that it saw the gap.
- Owners are notified (system inbound) only while `running` / `idling`, never resurrected; a shell reclaim notifies only when it interrupted a running job.
- The cadence stamp is the claim, not the outcome: a slow phase that fails waits out its interval, and a restart resumes the clocks instead of running every slow phase at once.
- Known gap, unchanged: a kill dispatched but cut off before its row is deleted loses the "interrupted" notice (the next round's kill answers `absent`, silent).

## Key Dependencies
- `agent_shell_ttls`, `web_sessions`, `maintenance_state` — the tables it reads and settles.
- `ops.cluster.rpc` — the `shell_kill` and `shell_probe` ops to runners' ops servers.
- `base/daemon/round_loop.py` — the round runner and bounded fan-out the resident service loops share (also the delivery watchdog).

## Entry Points
- `services/upkeep/ttl_reaper/daemon.py` — `.venv/bin/python -m services.upkeep.ttl_reaper.daemon`
- The application root supervises it through the roster's `/healthz` identity probe (`services/supervision/roster/healthz.py`).


## Process composition

The executable entry captures one `LoadedCommit` and retains its `CodeVersion`
and `ProcessDbGate`. The work database factory and the lazy telemetry producer
share that gate, so new handles preserve the process admission read budget.
Logging and the existing health endpoint receive the same captured image; a
checkout move cannot relabel the running daemon. The daemon's existing startup,
pidfile, work and bounded-exit order remains unchanged.

The service entry retains its prepared `ConfigBoot` and resolved unit runtime
path. Endpoint and pidfile operations borrow those inputs through
`ServiceEndpoints.from_boot`; each original lookup reads the same live view.
The work and logging database factory calls `db_config_from_boot` at each
handle creation and retains the entry's gate and lazy machine host reader.
Schema checks, health-before-pool ordering and shutdown stay at their original
boundaries. Business-specific configuration slices keep their existing readers.
