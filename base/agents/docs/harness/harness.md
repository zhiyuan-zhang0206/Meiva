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
enum, the column and its CHECK together, while retiring a harness follows
expand-contract (`kimi_code` remains in the DB CHECK in phase 1 so historical
rows are not rewritten or rejected on upgrade).

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
`ForkHarnessMismatch` for a cross-harness fork.

## One key in the overlay, one column in the row

A caller names the harness as `config_overlay["harness"]`, at spawn and at restart alike.
It is declared in `COLUMN_OVERLAY_KEYS` (`base/agents/model/overlay_columns.py`) and is
deliberately not a framework `Settings` field: it has no cluster default, no `.env` alias, no
lifecycle class, and no copy in `agents_meta.config_overlay` or `birth_config`.
`agents_meta.harness` stays the only place it is stored. Every reader takes the key out with
`split_overlay_columns`, which returns the parsed `Harness` (None when the key is absent, so each
caller keeps its own default: native for a plain spawn, the spawner's for an agent spawn, the
source's for a fork, the stored one for a restart) and the remaining config fields. An unknown
value, a non-string or a null raises `InvalidConfigOverlay`. `SpawnAgentRequest` does this at
validation (HTTP 422), so its `config` holds config fields only and `harness` is a derived value;
the config-field validators reject a `harness` key that was not taken out first as an unknown key.
## Birth settlement and restart switching

Harness is a property of the agent initialized at birth and switchable on restart:

- The SDK and the request schema reject an unknown name at the boundary
  (`meiva.agents.spawn` raises `InvalidConfigOverlay` before any request;
  `SpawnAgentRequest` refuses it while validating `config`, so HTTP answers 422).
  No path falls back to the default.
- The birth transaction settles the initial value
  (`base/agents/birth/transaction.py` calls `resolve_birth_harness` before it
  writes the row).
- On restart, an agent in `idling` or `terminated` status may switch harness via
  `POST /api/agents/{id}/restart` with `config_overlay={"harness": "..."}` and a
  mandatory `message`. The update trigger that once made the column immutable was retired to
  enable controlled operator and agent transitions. A running agent switches itself with
  `meiva.self.restart(message, config_overlay={"harness": ..., "llm_model": ...})`: the
  same validation, then a pending command the host applies when the turn ends
  ([decision](../../../../docs/decisions/agents/graph/2026-11-11-self-restart-at-turn-end.md)).

## Epochs and what a switch shares

`agents_meta.harness` is the harness the agent runs on now. `agent_harness_epochs` keeps the
history of it: one row per stretch of the agent's life on one harness (seq from 0, the harness and
model, the CLI session id once bound, `started_at` / `ended_at` with NULL for the current epoch,
the byte range of the CLI record that belongs to the epoch when there is one, who switched, and the
restart message truncated). It holds facts about when and where, never what was said. The birth
transaction writes epoch 0; a switch closes the current epoch, opens the next and updates
`agents_meta.harness` in one transaction; a restart that keeps the harness opens nothing. The
timeline page composes its earlier pages from these rows
([timeline](../../history/docs/timeline/timeline.md#timeline-across-a-harness-switch);
[decision](../../../../docs/decisions/agents/timeline/2026-11-25-continuous-timeline-across-harness-switch.md)).

**A switch shares no conversation.** The new harness starts with its own history and its own system
prompt and cannot see anything said or done before the switch. Meiva does not replay or summarize the
old conversation and appends no note about it to the built-in checkpoint. The restart message is the
only handoff: the caller (an operator through the SDK, CLI or REST API, or the agent itself through
`meiva.self.restart`; the web UI's Restart never switches) writes the handoff there and, when it is long, writes it to a file and points to the path.
The switch note the new harness receives says so in one sentence, and the SDK docstrings, the
`meiva-guide` agents skill and the CLI help carry the same rule. Switching back to a harness resumes
that harness's own session record (see
[the decision](../../../../docs/decisions/agents/graph/2026-11-12-harness-switch-keeps-session-records.md)),
which holds none of what happened on other harnesses in between.

## The fork rule

A fork keeps its source's harness. `meiva`, `claude_code` and `codex` sources fork (an external
one only onto its own machine, because the CLI session it clones is a local file; see
[forking an external harness agent](../../../lm/harness/docs/fork.md)). Naming another harness on a
fork, forking an external source onto another machine, or forking an `antigravity` source raises
`ForkHarnessMismatch` (`fork_harness_mismatch`, HTTP 400) in prechecks and the birth transaction,
rolling the birth back. A source with no launcher raises `HarnessNotLaunchable`.

## No launcher, no birth

Recording a harness no launcher serves would make the column a claim about a
runtime loop that is not running, so such an agent could not run until it was
switched onto another loop. The birth boundary refuses it, and two run-time
gates keep rows born before the rule inert. The set, both gates and what
"launchable" does not promise:
[launchable harnesses and the birth gate](launchable-and-birth.md).

## The availability probe

The local SDK reports installed/sign-in evidence per harness. Probe semantics and
explicit frontend refresh are owned by [Harness Availability](availability.md).

## Which models a harness runs

The model registry owns that matrix: a model declares the external harnesses that
can run it, and `GET /api/models` publishes the answer per model. The spawn form
filters its model column by it.
[Which models a harness runs](harness-models.md).

## Choosing a harness for a new agent

The spawn form reads the last per-machine snapshot and checks a runner only when
Refresh is pressed. See [Harness Availability](availability.md).
