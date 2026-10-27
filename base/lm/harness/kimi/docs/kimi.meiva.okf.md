---
type: doc
title: Kimi Code harness adapter
description: '`base/lm/harness/kimi/` — one Kimi Code (`kimi acp`) session as a turn engine: session start/resume, ACP update mapping (including reasoning chunks), cancel with process assertions, and per-turn usage read from Kimi Code wire logs.'
tags:
- base
- library
- llm-inference
- external-agents
---

# Kimi Code harness adapter

`base/lm/harness/kimi/` drives Kimi Code (`kimi acp`) as an external agent. It
speaks **ACP** — the Agent Client Protocol — over stdio through
[[../../acp/docs/acp.meiva.okf.md]]; the shared half (events, handoff, registry,
process probe) lives in [[../../docs/harness.meiva.okf.md]] and this adapter owns
only what Kimi Code decides differently.

## Entry points and ownership

- `KimiConfig` (`base/lm/harness/kimi/config.py`) — immutable configuration for
  the `kimi acp` child process (`cwd`, `kimi_home`, `binary="kimi"`,
  `subcommand=("acp",)`, `model`, `reasoning_effort`, `skip_permissions`,
  `permission_mode`, timeouts, queue bounds).
- `KimiSession` (`base/lm/harness/kimi/session.py`) — one ACP connection and one
  session (`initialize` → `session/resume` or `session/new` → optional
  `session/set_config_option`; a rejected `session/resume` fails fast instead of
  falling back to `session/new`). Queues inbound turns one at a time, emits
  `BridgeEvent` records, handles `session/request_permission` callbacks, cancels
  turns via `session/cancel` with descendant reaping, and settles process-tree
  teardown on `close()`.
- `map_update`, `assistant_text`, `reasoning_text`, `parse_prompt_result`
  (`base/lm/harness/kimi/mapping.py`) — pure translation of ACP `session/update`
  payloads (`agent_message_chunk` → `TextDelta`, `agent_thought_chunk` →
  `ReasoningDelta`, `tool_call` → `ToolUse`, `tool_call_update` → `ToolResult`)
  and `session/prompt` stop reasons.
- `read_usage`, `UsageSnapshot`, `StepUsage` (`base/lm/harness/kimi/usage.py`) —
  per-turn token usage extraction from Kimi Code's persisted wire logs under
  `<kimi_home>/sessions/<workspace>/<session>/` (`wire.jsonl`,
  `agents/main/wire.jsonl`, or `session.jsonl`), summing `input_other`,
  `input_cache_read`, and `input_cache_creation` into `Usage.input_tokens` per
  harness invariant 6.

## Who an update belongs to

A turn ends on the `session/prompt` **response**, matched by JSON-RPC id, so an
update Kimi Code sends on its own cannot end a turn. `session/update` carries no
turn id, so content attribution is weaker than the boundary: an update that
arrives while a turn is running is that turn's, and one that arrives with no turn
in flight is dropped and counted (`unclaimed_updates`) rather than carried into the
next turn. Mid-turn the two are indistinguishable — the documented residual risk,
not something this adapter guesses at.
