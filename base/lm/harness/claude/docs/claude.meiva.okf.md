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
command above (`MissingClaudeSdkError`). The development environment resolves
the SDK through type stubs (`typings/claude_agent_sdk/`), so pyright still
resolves the import without bundling the ~240MB CLI in the default checkout.

## Modules

| Module | Owns |
|---|---|
| `session.py` | the effect shell of `ClaudeSession`: start / resume / close, ordered inbound queue, one event stream, interrupt, totals — it awaits the SDK and holds the state, and every decision it makes comes from the modules below |
| `inbound.py` | what one inbound message is (dispatch by class name), which result may claim the running turn, what a step and a turn's text are, the background-task set, and whether a task report goes to the sink or the stream |
| `options.py` | the keyword values handed to the SDK's option class, including the two that ride only when resolved or supported |
| `settle.py` | how one result settles a turn: the outcome, the stream event, the new session id and the running totals |
| `mapping.py` | pure SDK-message → `BridgeEvent` translation, dispatched by class name |
| `auth.py` | the credential boundary: OAuth by default, API key only on request, secrets never returned |
| `config.py` | `BridgeConfig` |

The package door (`__init__.py`) re-exports the pure half; `ClaudeSession` and the errors it
exports come from `base.lm.harness.claude.session`, so the door itself stays `strict`.

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
| `tools` | `None` | retain the complete native CLI tool set; an explicit tuple restricts it |
| `permission_mode` | `bypassPermissions` | headless session with no approval layer: the CLI must not park on a prompt nobody can answer. The workspace is the boundary. |
| `max_turns` | `None` | retain native completion behavior without an artificial 24-step cutoff; the bridge deadline still bounds stalled turns |
| `turn_timeout_seconds` | 900 | a wedged CLI surfaces as a bridge fault instead of a hung agent |
| `cancel_settle_seconds` | 5 | observed reaping is ~40ms; 5s tolerates load without hiding a leak |
| `max_pending_turns` | 32 | overflow returns the message to its durable owner instead of growing memory |
| `max_buffered_events` | 256 | text deltas are droppable past this; control events are bounded by tool-call count |
| `dedupe_window` | 256 | a redelivery inside the window replays the outcome instead of re-running the turn |
| `injected_turn_timeout_seconds` | 30 | the CLI answers a background-task notification in a turn of its own; dispatch is held until that turn reports, and this bound turns a CLI that never reports into one wait instead of a wedged agent. Observed notification turns are two to three seconds, so 30 is an order above them and still bounds a user's wait |

Native user, project and local settings remain enabled, including CLAUDE.md,
skills, hooks, plugins and configured MCP servers. The adapter does not set
`setting_sources=[]` or strict MCP isolation. It adds no Meiva MCP configuration
and never overrides the provider system prompt. Model/effort selections override
only those execution choices; authentication still follows the credential boundary.

## Background tasks

A `Bash` command the model starts with `run_in_background=true` outlives its turn,
and the CLI reports its end on its own:
[[background-tasks.meiva.okf.md|background tasks in a Claude session]].

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
