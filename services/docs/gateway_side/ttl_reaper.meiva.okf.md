---
type: doc
title: TTL Reaper — wall-clock deadline enforcement
description: Gateway-side service that reclaims every row past its wall-clock deadline (pages, persistent shell sessions, browser sessions, notices, impersonation leases) and runs the slow lifecycle-pointer and fire-log maintenance on durable cadences.
tags: []
---

# TTL Reaper — wall-clock deadline enforcement

## What is it
The service that enforces every deadline the cluster hands out. It runs once per cluster on the gateway side (`ServiceSpec.capabilities=GATEWAY`, health port slot `ttl_reaper` = 8121), as its own root unit: stopping or restarting it never touches the HTTP gateway, and the gateway holds no loop that can kill a shell.

## Loops
Two resident sequential loops under one `TaskGroup` (`services/upkeep/ttl_reaper/daemon.py`), each running a round every `MEIVA_TTL_REAPER_POLL_INTERVAL_SECONDS` (default 60s). A loop that raises cancels its sibling and ends the process; the supervisor restarts it. An unreachable database skips a round. Each loop reports its own progress to `/healthz`.

- **`sweep`** (`sweep.py`) — database only: expired `agent_pages` (terminalized with `expired_at`, `PageClosed` published), `web_sessions`, `agent_notices`, impersonation leases (reminded, then reaped), plus three slow phases claimed in `maintenance_state` (`cadence.py`): the `schedule_fire_log` retention prune (daily), the torn lifecycle-pointer scan and the absent-machine fence settle (hourly, `lifecycle_fences.py`).
- **`remote`** (`remote.py`) — calls out, in two phases per round: TTL-expired persistent shell sessions are killed on their home machines (`shells.py`; machines concurrently, one machine's rows in order, each dispatch under a deadline sized from the RPC client's budget), then a stale sweep drops the tracking rows of sessions their home machine no longer lists (one `shell_probe` per (machine, agent), bounded to one batch of rows per round). Which rows go to which machine, how a runner's answer settles a row and what the owner is told are decided by the strict `shell_plan.py` and `owner_text.py` from values; the daemon reads `TtlReaperConfig` (poll interval, fire-log cadence and retention, shell-kill deadline, notice timezone) once from the boot owner and hands it to every loop.

## Semantics that matter
- A shell row is deleted only on a definitive verdict (`killed` / `absent` / `machine_absent`); runner results use `ShellKillMode` from `base/api_contracts/agent_contract.py` (`killed` / `absent`), while `machine_absent` requires local registry evidence and cannot come from a runner payload. Unknown or missing runner modes are logged and deferred; an unreachable machine, a failed op or a dispatch past its deadline leaves it for the next round. Each kill re-checks the row is still expired against `clock_timestamp()`, which pairs with the renewal guard so a renewed session is never killed.
- The deadline is not the only way a shell row becomes garbage: a session that dies with its host leaves its row until the declared deadline, and nothing else reads an unexpired row. The stale sweep settles those — one `shell_probe` per (machine, agent) lists the host's live sessions and the rows of the sessions it did not list are dropped — but only on a definitive answer (a listing, or a machine absent from the registry): an unreachable host, a failed op, a dispatch past its deadline or a malformed listing leaves every row alone, because dropping a live session's row leaves that session unreclaimable forever. A row is exempt for its first six hours, and the delete names the row's own `expires_at` and `created_at` so a row renewed or rewritten since the select survives. The sweep sends no owner notice: the session it described is already gone.
- Owners are notified (system inbound) only while `running` / `idling`, never resurrected; a shell reclaim notifies only when it interrupted a running job. Notice expiry is the one queue-anyway case: the message reports that owner's own notice, so a `terminated` owner keeps the row in its inbox — read if the agent is brought back within the stale-chat window (24h by default, `MEIVA_DELIVERY_WATCHDOG_STALE_CLAIMED_THRESHOLD_SECONDS`), retired as history past it — still with no wake and no resurrection, and such a row is excluded from the delivery-stall alert. Expiry is silent for the user (ruling 2026-10-21): the notice leaves the open queue and no notification is posted for it.
- The cadence stamp is the claim, not the outcome: a slow phase that fails waits out its interval, and a restart resumes the clocks instead of running every slow phase at once.
- Known gap, unchanged: a kill dispatched but cut off before its row is deleted loses the "interrupted" notice (the next round's kill answers `absent`, silent).

## Key Dependencies
- `agent_shell_ttls`, `agent_pages`, `web_sessions`, `agent_notices`, `maintenance_state` — the tables it reads and settles.
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
