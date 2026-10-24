---
type: doc
title: Exec-Host Service — supervised execute_code child process runner
description: "Per-machine agent-runner daemon (`exec-host`) that spawns and supervises disposable `agent.execution.child` process groups over `exec-host.sock` and runs the host memory guard."
tags: []
---

# Exec-Host Service

## What is it

`exec-host` (`services/agent_runner/exec_host/`) is the supervised `agent-runner` service that spawns and manages disposable `agent.execution.child` subprocesses on behalf of `agent-host`. It listens on `<home>/run/exec-host.sock` (`base/sessions/exec_host/paths.py`) and speaks the line-delimited JSON protocol defined in `base/sessions/exec_host/protocol.py`.

## Responsibilities

- **Readiness probe (`ping`)**: Answers `{"id": req_id, "method": "ping"}` immediately on the event loop so `services/supervision/healthchecks/protocol_probe.py` certifies socket ownership and responsiveness.
- **Subprocess execution (`run`)**: Validates that `request_path` and `result_path` reside inside `<home>/run/exec`, builds the child environment from the daemon's booted environment plus caller overlays (preserving `MEIVA_PERMISSIONS_HELPER_PID` for macOS TCC inheritance), spawns `python -I -B -X utf8 -m agent.execution.child` in its own session and process group via `ExecProcessDomain.launch_posix`, streams merged `stdout`/`stderr` as `output` frames, and returns `{"returncode": ..., "cancelled": ..., "timed_out": ...}`.
- **Cancellation, timeout, and disconnect cleanup**:
  - In-band `{"id": req_id, "method": "cancel"}` or client socket EOF triggers `SIGINT` to the process group followed by a bounded grace window (`KILL_GRACE_S`) and `domain.kill_and_reap()`.
  - Exceeding `timeout_s` triggers `SIGTERM` to the process group followed by the same grace window and `domain.kill_and_reap()`.
- **Memory guard**: Runs `run_memory_guard_forever` to monitor OS memory pressure and terminate the largest `execute_code` domain when system memory reaches `critical`, writing a notice next to the child's result file.

## Child environment authority

`base/sessions/exec_host/child_environment.py` owns the environment contract for
both direct children and socket-delegated children. The service merges its booted
environment with caller `env_overlay`, restores the daemon's
`MEIVA_PERMISSIONS_HELPER_PID`, then applies that contract. Ambient per-agent
config carriers are removed; only explicit, non-empty `config_overlay` and
`birth_config` maps become JSON carriers. Missing, null and empty maps leave the
child unbound to those carriers.

The child runs the daemon's interpreter. The daemon composition root therefore
supplies that interpreter's source checkout, independently of the caller's
checkout. A child whose cwd is outside that source checkout, or inside its
`.worktrees` or `.claude/worktrees`, drops `VIRTUAL_ENV` after the environment
merge so a bare `uv` command cannot select another checkout's editable install.
A system interpreter with no source checkout preserves its environment's venv
identity. Socket delegation does not change this policy.
