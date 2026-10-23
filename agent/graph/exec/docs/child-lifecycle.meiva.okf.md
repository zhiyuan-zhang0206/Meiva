---
type: doc
title: Exec Child Lifecycle — Spawn, Timeouts and Cleanup
description: How `run_in_subprocess` admits and spawns the disposable exec child (editable-install check, launch plan, poll beat), the two hard timeouts, and the single cleanup path every exit takes — group kill, reap, bounded pipe drain, request and result file deletion.
tags: []
---

# Exec Child Lifecycle — Spawn, Timeouts and Cleanup

## Responsibility

One disposable child process per `execute_code` call: admitted by an
editable-install check, spawned from a launch plan, polled until it ends, and
always torn down the same way. What the model sees of the run (streaming, the
envelope, result dispatch) is in
[[agent/graph/exec/docs/exec.meiva.okf.md|Tool Execution]]; the process
boundary the graph binds is [[agent/graph/exec/docs/process.meiva.okf.md|the child process boundary]].

## Spawn

- `run_in_subprocess` (`_subprocess.py`) first verifies the current interpreter's editable-install records, from the `ParentFacts` its caller read once (environment, cwd, interpreter, checkout root, allowed editable roots; `_child_boundary.read_parent_facts`). Invalid records refuse the call before any request file exists; the refusal text is `_guard.py`'s pure decision. The guard never repairs them; operators must inspect and converge the installation before new work. It then spawns one disposable child per exec, from the argv, environment and cwd that `plan_child_launch` (`_child_boundary.py`) returns as values (`python -I -X utf8 -m agent.execution.child`, same venv) — isolated mode keeps the inherited process cwd and `PYTHON*` environment out of trusted bootstrap import resolution, and explicit UTF-8 mode preserves portable text after `-I` ignores encoding env vars; a stuck native call is hard-killable without touching the agent process (issue #184); the parent polls every 50ms; what each beat does (forward text, keep alive, stop on cancel or deadline) is `_poll.plan_poll`'s decision. The child leads its own session and process group. Persistent `meiva.shell.sessions` live outside it, so they survive domain close

## Timeouts and cleanup

- Double-layered hard timeout: per-code `exec_timeout_seconds` (default 300s) + graph-level `exec_node_timeout_seconds` (default 1200s, defense in depth — the parent-side `asyncio.wait_for` above the whole run). The parent observes the leader's exit without reaping it. Cancel/timeout runs signal → grace → process-group kill. Every path (natural exit, cancel, timeout, error, task cancellation) then ends the same way: SIGKILL the group while the leader is unreaped, reap the leader, drain the output pipe for a bounded time, delete the request and result files; this covers `os._exit` and watchdog exits. The unreaped zombie leader pins its pgid, so cleanup never targets a recycled group number; once the leader is reaped nothing is signalled. The parent keeps the write end of the child's stdin pipe until the reap: if the parent dies first the child reads end-of-file and kills its own group ([[agent/execution/docs/child-boundaries.meiva.okf.md]])

## Entry Points

- `agent/graph/exec/_subprocess.py` — `run_in_subprocess`, the spawn / poll / cleanup driver
- `agent/graph/exec/_child_boundary.py` — `read_parent_facts` and `plan_child_launch`
- `agent/graph/exec/_guard.py`, `agent/graph/exec/_poll.py` — the editable-install refusal text and the poll-beat plan

Parent: [[agent/graph/exec/docs/exec.meiva.okf.md|Tool Execution]].
