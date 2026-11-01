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
five legal values: `meiva` (the built-in runtime), `claude_code`, `codex`,
`antigravity` and `kimi_code`. It is locked to the `agents_meta.harness` CHECK by
`tests/contracts/test_db_check_enum_sync.py`; adding a harness means editing the
enum, the column and its CHECK together.

## How a harness bills

`HarnessBilling` (`api` / `subscription`) sits beside the enum; `HARNESS_BILLING`
maps every harness to one and raises at import on a gap. The built-in runtime is
metered, the external CLIs are subscriptions. The harnesses route carries it per
row, and the spawn picker shows rates only for `api`: a subscription pays nothing
per token, and one model costs differently per loop (2026-10-20).

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

## Birth settlement and restart switching

Harness is a property of the agent initialized at birth and switchable on restart:

- The SDK and the request schema reject an unknown name at the boundary
  (`meiva.agents.spawn` raises before any request; `SpawnAgentRequest.harness` is
  a typed enum, so HTTP answers 422). No path falls back to the default.
- The birth transaction settles the initial value
  (`base/agents/birth/transaction.py` calls `resolve_birth_harness` before it
  writes the row).
- On restart, an agent in `idling` or `terminated` status may switch harness via
  `POST /api/agents/{id}/restart` with `config_overlay={"harness": "..."}` and a
  mandatory `message`. The prior immutable update trigger was retired to enable
  controlled operator and agent transitions.

## The fork rule

Forking (`fork_from`) is supported **only on the native `meiva` harness** — external
harnesses own their CLI session on disk and cannot be cloned. Forking a non-`meiva`
source or requesting a non-`meiva` harness on a fork raises `ForkHarnessMismatch`
(`fork_harness_mismatch`, HTTP 400) in prechecks and the birth transaction, rolling
the birth back. A source with no launcher raises `HarnessNotLaunchable`.

## No launcher, no birth

Recording a harness no launcher serves would make the column a claim about a
runtime loop that is not running, and the value never changes, so such an agent
could be neither run nor moved. The birth boundary refuses it, and two run-time
gates keep rows born before the rule inert. The set, both gates and what
"launchable" does not promise:
[[base/agents/docs/harness/launchable-and-birth.meiva.okf.md|launchable harnesses and the birth gate]].

## The availability probe

The local SDK reports installed/sign-in evidence per harness. Probe semantics and
explicit frontend refresh are owned by [[base/agents/docs/harness/availability.meiva.okf.md|Harness Availability]].

## Which models a harness runs

The model registry owns that matrix: a model declares the external harnesses that
can run it, and `GET /api/models` publishes the answer per model. The spawn form
filters its model column by it.
[[base/agents/docs/harness/harness-models.meiva.okf.md|Which models a harness runs]].

## Choosing a harness for a new agent

The spawn form reads the last per-machine snapshot and checks a runner only when
Refresh is pressed. See [[base/agents/docs/harness/availability.meiva.okf.md|Harness Availability]].
