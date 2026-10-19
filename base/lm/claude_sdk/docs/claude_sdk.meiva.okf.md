---
type: doc
title: Claude Agent SDK session bridge
description: '`base/lm/claude_sdk/` — one Claude Agent SDK session as a turn engine: ordered inbound queue, event mapping, approvals, interrupt with process assertions, usage accounting.'
tags:
- base
- library
- llm-inference
- claude-sdk
---

# Claude Agent SDK session bridge

`base/lm/claude_sdk/` turns one Claude Agent SDK session into the runtime's turn
engine. It is not a LangChain chat model and not a provider plugin: the SDK drives
a bundled Claude Code CLI, so a turn is an agentic loop with real tool calls, a
resumable transcript and a subscription-backed quota.

The bridge is optional infrastructure. The SDK is an optional extra
(`uv sync --extra claude-sdk`) because it bundles a ~240MB CLI binary, and no
production path calls the bridge yet — the wiring design is
[wiring the session into the turn model](../../../../docs/decisions/agents/graph/2026-10-19-claude-sdk-session-bridge.md).

## Modules

| Module | Owns |
|---|---|
| `session.py` | `ClaudeSession`: start / resume / close, the ordered inbound queue, the single event stream, interrupt, totals |
| `mapping.py` | pure SDK-message → event translation, dispatched by class name |
| `approval.py` | `can_use_tool` → publish → wait → settle, with the timeout that guarantees the wait ends |
| `auth.py` | the credential boundary: OAuth by default, API key only on request, secrets never returned |
| `process.py` | the child-process probe that makes a cancel assertable |
| `config.py` | `BridgeConfig`: model, tool allowlist, timeouts and bounds, each with its reason |
| `events.py` | the event vocabulary and the droppable/control split |

## Configuration

`BridgeConfig` carries every bound; each one exists for a reason:

| Field | Default | Reason |
|---|---|---|
| `model` | `claude-sonnet-5-5` | full id from the bundled CLI's model table; an alias would let an upgrade change the model |
| `tools` | Read/Grep/Glob/Edit/Write/Bash | the CLI default set is wider (web, notebook, agent tools); naming it keeps the capability surface reviewable |
| `allowed_tools` | Read/Grep/Glob | read-only tools skip the approval round-trip; every mutating tool still asks |
| `max_turns` | 24 | observed turns use 1–4; 24 stops a runaway loop long before a provider budget would |
| `approval_timeout_seconds` | 120 | an unanswered approval must end; expiry denies |
| `turn_timeout_seconds` | 900 | a wedged CLI surfaces as a bridge fault instead of a hung agent |
| `cancel_settle_seconds` | 5 | observed reaping is ~40ms; 5s tolerates load without hiding a leak |
| `max_pending_turns` | 32 | overflow returns the message to its durable owner instead of growing memory |
| `max_buffered_events` | 256 | text deltas are droppable past this; control events are bounded by tool-call count |
| `dedupe_window` | 256 | a redelivery inside the window replays the outcome instead of re-running the turn |

## Invariants

- **One turn at a time.** A message submitted mid-turn waits for the running
  turn's `ResultMessage`; writing it earlier is folded into the pending tool
  result, where the model treats it as injected content.
- **One consumer.** `events()` is a single stream; a second consumer steals events.
- **Approvals always end.** Policy, human, or timeout — the timeout denies.
- **Cancellation is asserted.** `interrupt()` reports whether the tool's child
  processes were reaped, and `settled=None` when the CLI pid is unobservable.
- **No credential handling.** The bridge blanks credential variables in the child
  environment and never reads, stores or forwards a secret.
- **The child environment is built, not inherited.** `ClaudeAgentOptions.env`
  merges over the process environment, so the blanking is explicit.

## Testing

`base/lm/claude_sdk/tests/` runs against `fake_sdk.py`, a scripted stand-in whose
classes carry the SDK's names and fields: turns, tool calls routed through
`can_use_tool`, interrupts and rate-limit events, with no CLI and no quota. The
real end-to-end run is `demos/claude-sdk-bridge/bridge_demo.py`, one command.
