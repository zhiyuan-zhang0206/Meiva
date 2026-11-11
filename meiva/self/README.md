# meiva.self — Agent Self

## What it is

`meiva.self` provides agents with self-control and identity information—who you are, which machine you are on, and how to manage your lifecycle.

## Core API (core SDK)

### Identity
- `AGENT_ID: int` — Your agent ID (read-only forwarding of an internal framework slot; `MEIVA_SDK_DISABLE` can remove the self namespace without affecting identity).
- `MACHINE_SPEC: MachineSpec` — `MachineSpec(name, description)`, a NamedTuple (still unpacks as `name, description`) for the current machine; `description` is free prose, empty if unset. `MACHINE_SPEC.name` is the current machine name and the default for `meiva.agents.spawn(machine=...)`.

Both machine values resolve lazily from the bound `MeivaContext.machine`. A bare SDK
process without a context rejects these reads. An explicitly bound context can
supply a machine without an agent, plugins, a database or execution state; a
context without its machine owner fails at first machine use. Attachments either
own an independent identity or borrow and restore the prior context's exact one.

### Lifecycle
- `attach(path, label=None)` — Look at any local media file yourself (user uploads, screenshots, downloaded or generated images — not only files generated this turn). The file is read when the current `execute_code` call finishes and the media message is appended right after its result, so the model sees it on its next step in the same turn ([Tool Execution — Fault-Isolated Code Execution](../../agent/graph/exec/docs/exec.md)); it stays in the conversation. The choice against [`meiva.understand`](../understand/README.md): attach puts the pixels in the agent's own context (it sees them, they keep costing context); `understand` has a separate model answer in text and keeps the context small. Gated by the model's media capability (ruling 2026-08-28): a text-only model has no attach contract at all (SDK docs omit the member, the call raises), and a file whose modality the model's attach set excludes is rejected at registration with the allowed set — never a silent text-note delivery. Limits are 20 MiB per file, 8 files, and 48 MiB per turn. Native harness only: an external harness agent has the CLI's own read tools, so its help omits the member and the call raises, naming them.
- `pause_heartbeat(duration: float)` — Pause idle heartbeat for `duration` seconds (0, 86400]. For waiting on a known external event only (watcher / peer reply / scheduled callback / user answer), never as a substitute for terminating when the work is done. Only suppresses heartbeats, real wake-ups still arrive; a later call replaces the window. Prefer the longest window that fits the wait.
- `compact(summary) → NoReturn` — Replace entire conversation history with summary, irreversible. First write durable state to MEMORY.md + shared memory pool, then write summary (first person, covering Requests/Progress/In flight/Dead ends/Pitfalls/Verbatim tail).
- `restart(message, *, config_overlay=None) → str` — Restart under the same identity. Non-blank `message` is required explaining the restart intent. `config_overlay` merges into persistent settings (model/effort edits) and its `harness` key switches the runtime. A native agent that stays native replaces its process now (the call never returns): model/effort edits validate against the final stored overlay and birth configuration under a row lock, and configuration and the restart inbound commit together; an invalid pair changes neither. Every other restart (an external harness, or any change of harness) is validated by the gateway, recorded as a pending command and applied by the host when the current turn ends ([decision](../../docs/decisions/agents/graph/2026-11-11-self-restart-at-turn-end.md)): a native agent that asked for it ends its execution, an external harness gets the receipt as the result and ends its own turn. A second request while one waits raises.
- `terminate() → NoReturn` — End current process — the normal last step when your task is done (never idle waiting for someone else to do it); state preserved, a message resurrects you.

## Implementation
`meiva/self/__init__.py` is a `none` shell: each lifecycle entry reads the process once (identity,
settings, the bound context, the database row) and runs what the strict modules return.
`meiva/self/lifecycle_plans.py` decides whether the harness may take the action (`harness_refusal`,
`stored_harness_refusal`, for `attach` and `compact`), whether the caller is an attached controller,
the overlay a restart stores with the receipt of a withdrawn model, and the texts of a compaction;
`meiva/self/restart_plans.py` decides where a restart goes (now, or at the turn boundary through the
gateway) and what it tells the caller; `meiva/self/plans.py` returns
the rows: `restart_statements`, `terminate_statement`, `compact_statement`,
`heartbeat_pause_statements` and the limit check `pause_duration`. `meiva/self/wake.py` then
publishes the wake on the agent's inbound channel from the redis handle, the agent id and the
configured events channel it is given.

## Role label

- `set_label(text)` — Set role label (stable role name, not task summary, not a status line; a task name is a fair label for an ephemeral worker); peers discover you via this label using `get_neighbors`, and your responsibility chain via `get_ancestors`. The label is also stated in your own agent-ID context note at every window establishment. An empty string clears it. A label set here is never replaced automatically.
- It calls `meiva.gateway_client.patch_label` (`PATCH /api/agents/{id}`, source `self`), so the gateway must be reachable. The gateway writes the label and the sticky `label_user_set` bit (the labeler does not overwrite it), records the `label_change` audit fact in the same transaction (attributed to `agent:<id>`, so a label set while a lease borrows the agent also lands in the lease's event log), and publishes `label_updated` so the fleet monitoring view refreshes. It does not depend on the Fleet plugin.

## Borrowed harness identity
An external harness process acts as this agent through `meiva.attach()`: the bridge writes one registration row, exports the handle, and the SDK borrows that identity on the process's first read that needs one. A native agent does not see `meiva.attach` and is refused if it calls it. See [harness attach](harness/docs/harness-attach.md).

## Key Dependencies
- [Agent Lifecycle](../../agent/docs/lifecycle.md) — underlying implementation of restart/terminate
- [Environment Variables](../../agent/docs/env-vars.md) — AGENT_ID environment variable passing

## Notes
`compact()` is irreversible—original conversation history is permanently lost. Must first write critical state to personal memory (workspace `memory/`) or workspace files before calling.
