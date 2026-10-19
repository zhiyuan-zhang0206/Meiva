---
type: doc
title: DeepSeek Harness adapter
description: '`base/lm/harness/deepseek/` — one dsh (`--profile acp`) session as a turn engine: session start/resume, ACP update mapping, cancel with process assertions, and per-turn usage read from the runtime log.'
tags:
- base
- library
- llm-inference
- external-agents
---

# DeepSeek Harness adapter

`base/lm/harness/deepseek/` drives DeepSeek Harness (dsh) as an external agent.
It speaks **ACP** — the Agent Client Protocol — over stdio through
[[../../acp/docs/acp.meiva.okf.md]]; the shared half (events, handoff, registry,
process probe) lives in [[../../docs/harness.meiva.okf.md]] and this adapter owns
only what dsh decides differently.

## The profile

`dsh --profile acp` is dsh's standard ACP server profile, and it is the only one
carrying the full turn contract. Measured on dsh 0.x (2026-10-19):

| Profile | Prompt | Cancel | Per-turn usage |
|---|---|---|---|
| `acp` | `session/prompt` | `session/cancel` notification | aggregate `usage_update` only |
| `sdk` | yes | **no cancel** | no |
| `headless` | yes | no | no; also ignores the in-app credentials |

So the bridge always starts `acp`, and `session/cancel` is sent as a
**notification**, never as a request (as a request it answers `-32601`).

## What ACP gives and does not give

ACP carries committed semantic blocks, not provider deltas. One
`agent_message_chunk` is a whole segment of the answer, so the bridge emits one
`TextDelta` per chunk for liveness plus one `AssistantText` per turn, and never
claims token-level granularity it was not given.

`agent_thought_chunk` maps to nothing: a thought is not output. `tool_call` and a
settled `tool_call_update` map to `ToolUse` and `ToolResult`, with the result
content previewed to 2000 characters — the model already has the full payload
through its own turn.

A turn's notifications and its `session/prompt` response travel on one wire but
reach the session through two queues, so settling yields one event-loop turn
before computing the answer text.

## Usage

The wire carries only an aggregate `usage_update` (`used` / `size` are context
occupancy, not tokens billed). Per-turn accounting therefore reads the runtime's
own log at `$DSH_HOME/sessions/<escaped cwd>/<session>/session.v4.jsonl.zstd`.
That is an internal format, and reading it is a risk taken deliberately: when the
file, the decoder or the field is missing the snapshot says
`per_turn_usage=False` with a reason, and the turn's numbers are absent rather
than zero. A caller that silently received zeros would price a turn at nothing.

Cost is computed only when the caller injects a `PriceBook` (`base/lm/pricing`):
the adapter never builds one, because a library does not own the process's model
catalog. Without one `cost_usd` stays `None` and `SessionTotals.cost_known` turns
False, so an unpriced session is never reported as free.

## Cancel

`session/cancel` settles the turn with `stopReason: cancelled`, but the runtime
does not reap what that turn spawned. `interrupt()` therefore snapshots the
runtime's descendants, kills what the turn started (SIGTERM, then SIGKILL), and
returns a `CancelReport` naming the survivors. A process tree that cannot be
observed is reported as `settled=None`, never as clean. Children that existed
before the turn started are left alone.

## Permissions

dsh asks for permission on tool calls. `permission_mode="allow"` answers with
`allow_once`; `ask` and `deny` answer with `reject_once` and record the tool in
the turn's `permission_denials`. Answering with nothing would be read as a
refusal anyway, so the adapter states it.
