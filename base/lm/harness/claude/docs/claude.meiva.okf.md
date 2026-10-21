---
type: doc
title: Claude harness adapter
description: '`base/lm/harness/claude/` — one Claude Agent SDK session as a turn engine: start/resume, handoff injection, event mapping, interrupt with process assertions, usage accounting. Behind the optional `harness-claude` extra.'
tags:
- base
- library
- llm-inference
- claude-sdk
---

# Claude harness adapter

`base/lm/harness/claude/` turns one Claude Agent SDK session into a turn engine.
It is not a LangChain chat model and not a provider plugin: the SDK drives a
bundled Claude Code CLI, so a turn is an agentic loop with real tool calls, a
resumable transcript and a subscription-backed quota.

The shared half — events, handoff, registry, process probe — lives in
[[../../docs/harness.meiva.okf.md]]. This adapter owns only what the Claude SDK
decides differently from another provider.

## Optional dependency

`claude-agent-sdk` is the `harness-claude` extra, not a base dependency: its wheel
bundles a ~240MB Claude Code CLI, and a machine that runs only Codex or
Antigravity should not pay for it.

    pip install 'meiva[harness-claude]'

`session.py` imports the SDK inside `_load_sdk()`, at session construction, so
`import base.lm.harness` and `import base.lm.harness.claude` both work on a
machine without the extra; only building a session fails, and the error names the
command above (`MissingClaudeSdkError`). The development environment carries the
package through the dependency group, so pyright still resolves the import.

## Modules

| Module | Owns |
|---|---|
| `session.py` | `ClaudeSession`: start / resume / close, ordered inbound queue, one event stream, interrupt, totals |
| `mapping.py` | pure SDK-message → `BridgeEvent` translation, dispatched by class name |
| `auth.py` | the credential boundary: OAuth by default, API key only on request, secrets never returned |
| `config.py` | `BridgeConfig` |

## Three responsibilities

1. **Start / resume the session.** `ClaudeSession.start()` opens the session and
   writes the CLI's own working surface: no system-prompt override, no approval
   callback, no MCP servers of ours. `resume` comes from the caller or from the
   registry.
2. **Inject the handoff as the first message.** The session submits the rendered
   handoff as its first turn on a fresh session. A resumed session is **not**
   re-injected: its transcript already carries it.
3. **Map events back and persist the identity pair.** `events()` is the single
   outbound stream, and the registry writes `agent_id ↔ session_id` as soon as
   the SDK reports its init.

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

## Claude-specific invariants

- **One turn at a time.** A message submitted mid-turn waits for the running
  turn's `ResultMessage`; writing it earlier is folded into the pending tool
  result, where the model treats it as injected content.
- **Cancellation is asserted.** `interrupt()` reports whether the tool's child
  processes were reaped, and `settled=None` when the CLI pid is unobservable.
- **The child environment is an allowlist.** `auth.build_env` names every key the
  session may carry (`registry.session_child_env_keys`) and blanks every other key
  the host process holds, because the SDK spawns the CLI with
  `{**os.environ, **options.env}` — a key left out of the mapping is filled back in
  from the parent, so omission is not removal. The inherited model credentials are
  therefore empty in the session: before this rule a claude session's Bash tool
  could read the host's `OPENAI_API_KEY` (#5512). The two Claude credential
  variables stay blanked rather than dropped, and API-key mode restores exactly the
  one the caller names; the adapter never reads, stores or forwards a secret. The
  registration handle and the runner's home ride because they are on the allowlist —
  a tool the CLI spawns needs the handle to attach as the agent it belongs to.
- **No system-prompt override.** `system_prompt` is not set; the CLI's own preset
  stands. The handoff is a message, not a prompt edit.
- **A killed host leaves the CLI behind.** If the host is SIGKILLed, the bundled
  CLI reparents to init and keeps the session; a supervisor must sweep it by the
  session id in the registry.

## Testing

`base/lm/harness/claude/tests/` runs against `fake_sdk.py`, a scripted stand-in
whose classes carry the SDK's names and fields: turns, tool calls, interrupts and
rate-limit events, with no CLI and no quota. The real end-to-end run is
`demos/claude-sdk-bridge/bridge_demo.py`, one command.
