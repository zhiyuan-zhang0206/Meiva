---
type: doc
title: Claude Agent SDK session bridge
description: '`base/lm/claude_sdk/` — one Claude Agent SDK session as a turn engine: start/resume, handoff injection, event mapping, persisted agent-id ↔ session-id, interrupt with process assertions, usage accounting.'
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

## Three responsibilities

1. **Start / resume the session.** `ClaudeSession.start()` opens the session and
   writes the CLI's own working surface: no system-prompt override, no approval
   callback, no MCP servers of ours. `resume` comes from the caller or from the
   registry below.
2. **Inject the handoff as the first message.** `Handoff` renders the six-section
   document a waking external agent reads, and the session submits it as its first
   turn on a fresh session. A resumed session is **not** re-injected: its
   transcript already carries it.
3. **Map events back and persist the identity pair.** `events()` is the single
   outbound stream, and `registry.py` writes `agent_id ↔ session_id` as soon as
   the SDK reports its init — that pair is what `resurrect` needs.

## Modules

| Module | Owns |
|---|---|
| `session.py` | `ClaudeSession`: start / resume / close, ordered inbound queue, one event stream, interrupt, totals |
| `handoff.py` | the six-section handoff document and its byte size |
| `registry.py` | `SessionRecord` + `FileSessionRegistry` (one document per agent, atomic replace) |
| `mapping.py` | pure SDK-message → event translation, dispatched by class name |
| `auth.py` | the credential boundary: OAuth by default, API key only on request, secrets never returned |
| `process.py` | the child-process probe that makes a cancel assertable |
| `events.py` | the event vocabulary and the droppable/control split |
| `config.py` | `BridgeConfig` |

## The handoff document

Fixed section order, because the external agent reads it once per wake and its
size is a per-wake cost:

| # | Section | Content |
|---|---|---|
| 1 | Identity | agent id, label, machine, workspace |
| 2 | Cluster | machine names and roles — only the ones this agent needs |
| 3 | Entry points | a short `import meiva` index, a purpose per entry (not full docstrings) |
| 4 | Do not use | the external agent's own background/monitor machinery, out-of-workspace edits, MCP servers |
| 5 | Reporting | one reply per turn, evidence over narration, say what could not be checked |
| 6 | Task | the actual instruction |

Sections 4 and 5 carry defaults (`DEFAULT_OVERLAPS`, `DEFAULT_DISCIPLINE`) and are
overridable. The rendered size is reported as `Handoff.size_bytes()` and as a
`HandoffInjected` event so the per-wake cost stays visible.

## Session identity

`FileSessionRegistry` stores one JSON document per agent id under its root:

```json
{"agent_id": 6984, "session_id": "4cd6f58f-…", "cwd": "…", "model": "claude-sonnet-5-5",
 "created_at": "2026-10-19T08:36:00+00:00", "updated_at": "2026-10-19T08:36:21+00:00", "resumes": 1}
```

Writes are temp-file plus `Path.replace`, so a reader never sees a half-written
record. A corrupt or incomplete document raises instead of silently starting a
fresh session under an id that already has history.

## Configuration

| Field | Default | Reason |
|---|---|---|
| `model` | `claude-sonnet-5-5` | full id from the bundled CLI's model table; an alias would let an upgrade change the model |
| `tools` | Read/Grep/Glob/Edit/Write/Bash | the CLI default set is wider (web, notebook, agent tools); naming it keeps the capability surface reviewable |
| `permission_mode` | `bypassPermissions` | headless session with no approval layer: the CLI must not park on a prompt nobody can answer. The workspace is the boundary. |
| `max_turns` | 24 | observed turns use 1–4; 24 stops a runaway loop long before a provider budget would |
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
- **Cancellation is asserted.** `interrupt()` reports whether the tool's child
  processes were reaped, and `settled=None` when the CLI pid is unobservable.
- **No credential handling.** The bridge blanks credential variables in the child
  environment and never reads, stores or forwards a secret.
- **No system-prompt override.** `system_prompt` is not set; the CLI's own preset
  stands. The handoff is a message, not a prompt edit.

## Testing

`base/lm/claude_sdk/tests/` runs against `fake_sdk.py`, a scripted stand-in whose
classes carry the SDK's names and fields: turns, tool calls, interrupts and
rate-limit events, with no CLI and no quota. The real end-to-end run is
`demos/claude-sdk-bridge/bridge_demo.py`, one command.
