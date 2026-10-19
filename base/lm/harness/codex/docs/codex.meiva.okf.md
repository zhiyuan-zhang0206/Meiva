---
type: doc
title: Codex app-server session bridge
description: '`base/lm/harness/codex/` — one `codex app-server` session as a turn engine: thread start/resume, JSON-RPC streaming, approval requests, per-turn usage, interrupt with process assertions, agent-id ↔ thread-id persistence.'
tags:
- base
- library
- llm-inference
- codex
---

# Codex app-server session bridge

`base/lm/harness/codex/` turns one `codex app-server` process into the
runtime's turn engine, alongside the Claude Agent SDK bridge. It is not an MCP client
and not a provider plugin: the app server runs the agent loop locally, so a turn
is a real agentic loop with tool calls, a resumable thread and a
subscription-backed quota.

**Codex has no ACP server.** No `acp` subcommand, `--acp` rejected, no protocol
symbols in the binary. The ACP packages in the ecosystem are third-party
adapters that lag the CLI, so the bridge speaks the app-server protocol
directly: the uniform surface is this adapter, not a shared wire protocol.

## Three responsibilities

1. **Open the thread.** `CodexSession.start()` spawns `codex app-server --listen
   stdio://`, performs `initialize` + `initialized`, then `thread/start` — or
   `thread/resume` with the recorded thread id. A rejected resume degrades to a
   new thread and re-injects the handoff: the transcript is gone, so the agent
   must be told what it is again.
2. **Inject the handoff as the first message.** `Handoff` renders the
   six-section document; a fresh thread submits it as its first turn. A resumed
   thread is not re-injected.
3. **Map notifications back and persist the identity pair.** `events()` is the
   single outbound stream, and the registry writes `agent_id ↔ thread_id`.

## Modules

| Module | Owns |
|---|---|
| `session.py` | `CodexSession`: start / resume / close, ordered inbound queue, one event stream, approvals, interrupt, totals |
| `transport.py` | `StdioAppServer`: JSON-RPC framing over the child's stdin/stdout, id correlation, server→client requests |
| `mapping.py` | pure `ThreadItem` → event translation and usage extraction |
| `config.py` | `CodexConfig` and the command line |

`events.py`, `handoff.py`, `process.py` and `registry.py` are the shared bridge
modules; this package does not restate them.

## Notification mapping

| app-server method | Bridge event |
|---|---|
| `item/agentMessage/delta` | `TextDelta` |
| `item/completed` (`agentMessage`) | `AssistantText`; the `final_answer` phase also becomes the turn's reply text |
| `item/started` / `item/completed` (`commandExecution`) | `ToolUse` / `ToolResult` (`is_error` when the status is failed/declined or the exit code is non-zero) |
| `item/started` / `item/completed` (`fileChange`) | `ToolUse` / `ToolResult` named `patch` |
| `item/*` (`mcpToolCall`, `dynamicToolCall`, `webSearch`) | `ToolUse` / `ToolResult` |
| `thread/tokenUsage/updated` | per-turn `Usage`, accumulated into `SessionTotals` |
| `turn/completed` | `TurnCompleted` (`status` → `subtype`, `interrupted` from the interrupt flag or status) |
| `error` with `willRetry=false` | `SessionFaulted` |

Everything else (reasoning, plan, image, sub-agent, queue and rate-limit
events) maps to nothing: the runtime does not model it.

## Approvals

An `untrusted`/`on-request` policy makes the server send requests the bridge
must answer on the same pipe:

| request | answer |
|---|---|
| `item/commandExecution/requestApproval` | `{"decision": "accept" \| "decline"}` |
| `item/fileChange/requestApproval` | same vocabulary |
| `applyPatchApproval` (legacy) | `{"decision": approved \| denied}` |
| `item/permissions/requestApproval` | grants nothing (`{"permissions": {}, "scope": "turn"}`) |
| `item/tool/requestUserInput` | empty answers |

`approval_decision` is the single knob: `accept` (default) or `decline`. A
declined request is recorded as `TurnCompleted.permission_denials`, so "the model
said it did not run the command" is checkable rather than trusted.

## Sandbox: why `danger-full-access`

`codex` runs commands through bubblewrap. Ubuntu 24.04 ships
`kernel.apparmor_restrict_unprivileged_userns=1`, so bwrap cannot create a user
namespace and **every** command in `read-only` or `workspace-write` fails with
`bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted` — the model sees a
sandbox-start failure, not a command result.

The bridge therefore defaults to `danger-full-access` and treats the workspace,
not the sandbox flag, as the boundary. `workspace-write` works only on a host
without that AppArmor restriction; on Ubuntu it degrades to "no command runs".

`approval_policy` defaults to `never` for the same reason a headless bridge
cannot park: nobody is at the prompt. With `danger-full-access` and `never`,
`codex` never asks, and the bridge's approval path stays dormant until a caller
opts into `untrusted`/`on-request`.

## Configuration

| Field | Default | Reason |
|---|---|---|
| `model` | `None` | inherit the machine's `codex` config; pinning an id the account does not serve fails every turn |
| `sandbox` | `danger-full-access` | see above: the only sandbox that runs on Ubuntu 24.04 |
| `approval_policy` | `never` | headless: no prompt to park on |
| `approval_decision` | `accept` | only consulted when the policy produces requests |
| `binary` / `extra_args` | `codex` / `()` | the CLI on `PATH`; tests substitute a peer |
| `startup_timeout_seconds` | 120 | `initialize`/`thread/*` are local round trips; a wedged server is a fault |
| `turn_timeout_seconds` | 900 | a wedged turn becomes an interrupt, never a silent hang |
| `cancel_settle_seconds` | 5 | reaping is milliseconds; 5s tolerates load without hiding a leak |
| `max_pending_turns` | 32 | overflow returns the message to its durable owner instead of growing memory |
| `max_buffered_events` | 256 | text deltas are droppable past this; control events are bounded by tool-call count |
| `dedupe_window` | 256 | a redelivery inside the window replays the recorded outcome |

## Invariants

Invariant 1 is how the queue is kept out of Codex's steering path: the session
calls `turn/start` **only after `turn/completed`**, so a turn is never started
against a busy thread. That matters because `turn/start` on a busy thread steers
the running turn instead of starting a new one — a message submitted mid-turn
would land inside a tool result, which is exactly what invariant 1 forbids.

The seven adapter invariants are pinned by tests in `tests/`:

| # | Invariant | Test |
|---|---|---|
| 1 | inbound only at a turn boundary | `test_inbound_queue.py::test_messages_wait_for_the_turn_boundary` |
| 2 | same key → same ticket; completed key replays | `test_inbound_queue.py::test_same_key_returns_the_same_ticket`, `::test_completed_key_replays_the_recorded_outcome` |
| 3 | a full queue refuses rather than buffers | `test_inbound_queue.py::test_queue_overflow_is_refused` |
| 4 | interrupt asserts the tool children are reaped | `test_cancel.py::test_interrupt_asserts_the_tool_children_are_reaped` |
| 5 | a turn timeout interrupts | `test_cancel.py::test_turn_timeout_interrupts_the_turn` |
| 6 | usage accumulates per turn | `test_inbound_queue.py::test_usage_accumulates_across_turns` |
| 7 | the handoff is injected once, never on resume | `test_session.py::test_resume_uses_the_registry_and_skips_the_handoff` |

## Version pinning

The CLI versions the protocol. `codex app-server generate-json-schema --out DIR`
writes the installed binary's exact schema — generate it at build time and diff
it. 0.163.0 removed the legacy `newConversation` / `sendUserTurn` RPCs, so a
client written against older docs fails outright.

The adapter uses the **stable surface only** (105 client methods on 0.163.0); the
experimental surface is not adopted.

Authentication is the machine's `codex login` (ChatGPT subscription). The bridge
never touches `OPENAI_API_KEY`: an API-key session has a different billing path
and no plan quota, and a stale key must not outrank the stored login.
