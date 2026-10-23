---
type: doc
title: Guarded Agent Creation
description: Principal-bound immutable agent and fork acceptance with original-attempt recovery.
tags:
- gateway
- agents
---

# Guarded Agent Creation

`POST /api/keyed/v1/agents` accepts creation and forks through the existing birth
transaction and launch owner. Every request requires a valid `Idempotency-Key`
(1–128 characters), the exact `Idempotency-Scope: principal-v1` header, and a
verified authenticated principal. Missing or invalid admission, an unverified
principal are rejected before birth or launch effects. Existing
authentication still applies to replay; revoked credentials cannot retrieve a
receipt.

The receipt identity includes the principal, POST method, and this versioned
logical path. Identical keyed requests replay the committed birth; changed
request data returns 409. The birth owner commits an immutable
`agent_creation_snapshots` record with the agent row, first prompt and audit
event in one transaction. Its manifest retains
the original agent ID, machine, config overlay, birth config and launch attempt
UUID. The request digest still uses the existing parsed `SpawnAgentRequest`;
this does not change the namespace or adopt guarded drafts' raw semantic identity.
Acceptance does not prove native execution.

Replay reads this retained birth before mutable placement and model checks. Only the original attempt, still idling, never admitted and on its
original machine, can receive the existing launch wake. A deliberate launch
retry, changed placement, admission, termination or deleted metadata returns
the original historical acceptance without forwarding a launch. Replay never
rotates an attempt, restores operator config or creates another first prompt.
The retained snapshot has no target FK or automatic expiry. This is the only
agent creation route: the browser, IM bridge, CLI, scripts, SDK and examples all
use it, and every other creation entry (guarded drafts, the MCP tool and task
assignment) records its birth through the same snapshot owner.

The SDK requires a caller key for every creation or fork and always uses this
path. The former `require_idempotency` mode flag has been removed. Forks use the
existing birth owner: source checkpoint resolution occurs for first acceptance,
then checkpoint copying, the fork marker, first prompt, immutable receipt and
audit commit together. Same-key replay reads the retained child first and does
not resolve or copy a later source checkpoint. See the SDK owner at
`meiva/agents/docs/strong-creation.meiva.okf.md`.

The gateway MCP tool `spawn_agent_guarded_v1` forwards to this route with the
caller's key and credentials, so it shares the route's principal namespace and
retained original birth recovery. See the
[MCP endpoint owner](../../mcp_server/docs/mcp-endpoint/mcp-endpoint.meiva.okf.md).

## Decisions and effects

`gateway/agents/creation.py` is the shell: it reads the registry and the agent row, runs the
birth transaction and dispatches the launch. Every decision that needs no effect is a pure
function in a strict module, tested by value in `gateway/agents/tests/`:

- `launch_plan.py`: which refusal a spawn target earns from its registry row (no
  `agent-runner` capability, paused), the `spawn-launch` payload, and the typed error an
  unreachable, rejecting or malformed runner turns into.
- `fork_plan.py`: what a fork's config overlay may differ in from its source, and the skills
  it adds at the context tail.
- `launch_state.py`: the verdict a failed dispatch is reported with (admitted anyway, retry
  legal, the 502 with its retry path) and the receipt of an accepted launch.

A test serves the target runner's `/ops` answer from a real listener and registers the machine
(`gateway/tests/support/runner_ops`), so the registry reads, the dispatch and the result handling run;
the gateway's own machine is registered by the `gateway_local_runner` fixture, whose route
executes the real op code. Races between requests for one key are produced by holding the
key's birth lock (`gateway/tests/support/db_faults/held_lock.py`); a failed write is a trigger in the
test's Postgres, never a replaced function.

Creation, SDK and immutable fork recovery tests live in `gateway/tests/lifecycle/creation/`.
Its local `path_scopes.toml` retains the creation identity fixture scope.
