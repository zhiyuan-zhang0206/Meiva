---
type: doc
title: Agent harness identity
description: Immutable runtime-loop selection at birth, the fork rule that keeps it, the local availability probe and the per-machine read behind the spawn form.
tags: [base, agents]
---

# Agent harness identity

A **harness** is the shell that runs the agent loop; the **model** and its
**provider** are separate choices. `base/agents/birth/harness.py` owns the
vocabulary and the two rules that settle a value: `resolve_birth_harness` decides
what a birth gets, `probe_local_harnesses` reports what this machine can run.

## The value set and where it lives

`Harness` (`base/api_contracts/agent_contract.py`) is the source of truth for the
four legal values: `meiva` (the built-in runtime), `claude_code`, `codex` and
`antigravity`. It is locked to the `agents_meta.harness` CHECK by
`tests/contracts/test_db_check_enum_sync.py`; adding a harness means editing the
enum, the column and its CHECK together.

`agents_meta.harness` is `NOT NULL DEFAULT 'meiva'`. Every row that predates the
column was born on the built-in runtime, which is exactly what the default and
migration `20261019T092549_agents-meta-harness.sql` record.

The birth boundary carries the value: `create_agent_row(harness=...)` takes a
plain spawn's requested harness (None = the built-in runtime) and lets a fork
inherit its source's, then `insert_agent_birth` writes it and raises
`ForkHarnessMismatch` for a cross-harness fork. Only the SDK/spawn path names a
harness today; the MCP tool endpoint and the task-assignment router build their
bodies without one and therefore always birth on `meiva`, which is what their
callers run.

## Immutable, enforced three times

Harness is a property of the agent, not of a run, so nothing may change it:

- The SDK and the request schema reject an unknown name at the boundary
  (`meiva.agents.spawn` raises before any request; `SpawnAgentRequest.harness` is
  a typed enum, so HTTP answers 422). No path falls back to the default.
- The birth transaction settles the value once
  (`base/agents/birth/transaction.py` calls `resolve_birth_harness` before it
  writes the row).
- A `BEFORE UPDATE OF harness` trigger (`agents_meta_harness_immutable`) raises
  on any UPDATE that would change it. Restart, resurrect, rollout and manual
  repair therefore cannot move an agent onto another runtime; correcting a wrong
  value means a new agent.

## The fork rule

A fork continues its source's identity, so it runs the source's harness. Omitting
`harness` on a fork inherits it; naming the source's own value is accepted; naming
a different one raises `ForkHarnessMismatch` (`fork_harness_mismatch`, HTTP 400)
and rolls the birth back. The comparison happens inside the birth transaction, so
a caller that bypassed the gateway's request validation still cannot fork across
harnesses. Spawning a new agent is the supported way onto another harness.

## Running on the harness

Recording a value nobody can honour would make the column a claim about a runtime
loop that is not running. `LAUNCHABLE_HARNESSES` names the harnesses a launcher
exists for — `meiva`, `claude_code` and `codex` — and two gates refuse to run
anything else:

- `validate_launch_row` (`ops/lifecycle/launch.py`) refuses the spawn's launch op
  with a reason naming the harness, so the caller sees why;
- `is_runnable` (`services/agent_runner/agent_host/scheduling/wake_screening.py`)
  refuses the hosted turn itself. The launch op is only one way into a turn: the
  delivery watchdog, the durable pending scan and pub/sub wakes all reach
  `_run_turn` without it, and every one of them passes this gate.

The spawn still records the request and the row keeps the value. It stays inert —
no turn starts on it, so no built-in loop runs under an external label — and
`retry_launch` can wake it once its launcher lands. Ending it in the meantime
needs a force terminate: a graceful terminate is applied inside a turn the row
cannot get. Adding a launcher means adding its harness to that set; removing one
is the rollback, and the rows it served go inert rather than falling back to the
built-in loop.

A launchable harness is not a working one: the wake gate says the release has a
launcher, and the session itself is what fails first if the CLI or its SDK is
missing (`agent/graph/docs/harness-turn.meiva.okf.md`).

## The availability probe

`meiva.agents.harness_availability()` reports one row per harness for the machine
the caller runs on. Each row carries a status — `available` (installed with a
credential marker present), `unavailable` (not installed, or installed with a
marker holding no credentials) or `unknown` (installed, but the sign-in state
cannot be read here) — plus a readable reason, the resolved executable path
(symlinks followed, so not always the name on PATH) and whether PATH led to it.
The probe reads that a credential file exists and is shaped like a session; it
does not validate freshness, so an expired credential still reads as `available`.

The probe reads the filesystem only. It runs no CLI, so it cannot hang on a login
prompt, spend quota or start a session. When PATH misses a user-level install the
probe still looks in the conventional bin directories, because an agent process
starts without the interactive shell profile; `on_path=false` says that is how the
executable was found. A harness whose credentials are kept outside those files
(Antigravity's CLI keeps its session elsewhere) reports `unknown` rather than an
invented answer.

Recording and validating the harness is one slice: the launchers that actually
start a session on an external harness are separate, and they read this column
rather than re-deriving the choice.

## Choosing a harness for a new agent

The spawn form asks the machine that would run the agent, not the gateway:
`GET /api/cluster/machines/{name}/harnesses` dispatches the `harness_probe` op to
that machine's runner, which answers from `probe_local_harnesses()` — a fresh read
every call, no runner-side cache. The gateway caches one answer per machine for 30
seconds, so a form reopened inside that window does not reach the op at all; that
cache belongs to the gateway because it is the only caller. A name no machine
carries answers 404, and a machine without an agent-runner (it could not birth an
agent there at all) answers 503 — the form renders either as `unknown`, never as
"not installed".

Each row carries the probe's `status` and `reason` unchanged, plus `launchable`,
which the gateway composes from two facts: a launcher for that harness exists in
this build (`LAUNCHABLE_HARNESSES`) and the machine can actually run it
(`available`, or the built-in runtime, which needs no CLI). A harness can
therefore be installed and signed in and still not launchable. The form keeps
such a row pickable, states the reason, and refuses the spawn — creating an agent
the machine cannot start would only look like success.
