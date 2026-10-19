# Wiring the Claude Agent SDK session into the turn model

## Context

`base/lm/claude_sdk` (v0.1) drives one Claude Agent SDK session: start / resume /
close, one inbound message per turn, a handoff document injected as the first
message, one event stream, an asserted interrupt, and `agent_id ↔ session_id`
persistence. It is deliberately not connected to the agent loop yet. This note is
the design for connecting it.

The user's 2026-10-19 16:40 ruling fixed the scope: external agents run **Python
code mode** (no MCP server), the bridge **does not touch Claude Code's system
prompt**, there is **no bridge-level permission layer**, and the agent id must be
stable and bound to the external session history because `resurrect` needs it.

The spike (`~/.meiva/workspaces/6982/spike/REPORT.md`) established the facts the
design rests on: the SDK runs on a subscription login without an API key;
`session_id` resumes across processes; `interrupt()` returns in ~5 ms and the CLI
reaps the tool's child processes itself (~40 ms observed); and a `query()` issued
mid-turn is folded into the pending tool result, where the model treats it as
injected content and refuses it.

## Decision

The bridge replaces the *model call*, not the graph. `agent/graph/llm` keeps
owning the turn's shape; only the body of one LLM round changes.

### How a message gets in

The claim node already owns the durable inbound queue and keeps that role. It
gains one step: instead of handing user text to a chat model, it calls
`ClaudeSession.submit(text, key=inbound_row_id)`.

- **One claim batch = one bridge turn.** The `ResultMessage` is the turn
  boundary, exactly as the graph's `BEFORE_LLM → AFTER_EXEC` cycle is today.
- **The idempotency key is the inbound row id**, so a gateway redelivery inside
  `dedupe_window` replays the recorded outcome.
- **Backpressure stays where the durable copy is**: `InboundQueueFullError` means
  the claim node leaves the row unclaimed and retries.
- The claim node does not submit while a bridge turn runs; it waits for the
  boundary, which is the same condition it uses today.

### How the session is woken

On a fresh start the caller passes a `Handoff`; the session submits the rendered
document as its first turn, so the external agent's first context is the six
sections (identity, cluster, entry points, do-not-use, reporting, task). On a
resume the handoff is **not** re-sent — the transcript already carries it.

Which cluster facts and which excerpts from our system prompt go in sections 2
and 3 is decided by the caller (the waking path), not by the bridge: the bridge
takes text, it does not read `agent/graph/prompt`. That keeps the selection
reviewable in one place and the bridge free of the prompt layer.

### How the identity pair is persisted

`FileSessionRegistry` writes `agent_id ↔ session_id` as soon as the SDK reports
its init, and refreshes `updated_at` on every turn. Resurrect reads that record:

```python
record = registry.load(agent_id)          # None → the agent has no session yet
await session.start(handoff=handoff)      # resumes record.session_id when present
```

The agent id is the durable identity; the session id is the transcript. Neither
is derivable from the other, which is why the pair is stored rather than
recomputed.

### How output leaves

A single consumer drains `session.events()` for the session's life and maps each
event onto what the graph already produces:

| Bridge event | Destination |
|---|---|
| `TextDelta` | the existing token stream (SSE) |
| `AssistantText` | the assistant message body for the round |
| `ToolUse` / `ToolResult` | the tool message + timeline event `execute_code` produces today |
| `TurnCompleted` | the round's final `AIMessage` (`text`), plus usage/cost |
| `RateLimitObserved` | the usage signal behind the quota warning |
| `SessionStarted` / `SessionBound` | binds the incarnation to the session id and the agent id |
| `HandoffInjected` | the per-wake cost record (size in bytes) |
| `SessionFaulted` | the llm node's retry path |

The SDK's tool surface is a real tool call rather than a Python namespace, so the
wiring maps one `ToolUse`/`ToolResult` pair onto the existing exec-node message
pair, and `BridgeConfig.tools` must name the same capability set the exec node
offers — otherwise the brain and the hands disagree.

### How cancel is sent

Today's native cancel path (`agent/ownership/native_cancel.py`) owns the lifecycle
decision; the bridge gets one call from it:

```python
report = await session.interrupt()
if report.settled is not True:
    ...   # residue, or an unobservable CLI — surface it, never assume success
```

`settled is None` (no observable CLI pid) is not a pass; the wiring records it as
unknown. This is the point impersonation used to get wrong, so the assertion lives
in the bridge rather than in a caller's memory.

### How cost is recorded

`TurnCompleted` carries the SDK's per-turn `usage` and `total_cost_usd`;
`SessionTotals` carries the running sum. The wiring writes the same `llm_usage`
row the LangChain path writes (`base/lm/usage.py`), with `usage_kind="agent"` and
`source="claude_sdk"` so the two runtimes stay comparable.

Under OAuth, `total_cost_usd` is the **list price**, not money paid. The real
budget signal is `RateLimitObserved` (`utilization`, `resets_at`), which the wiring
feeds to the quota monitor instead of adding dollars.

### Where it runs

Whichever machine holds the Claude subscription login — today macmini. That is a
**credential constraint, not a capacity constraint**: the executing machine is
chosen by where `auth status` reports `authMethod: claude.ai`. An agent on another
machine reaches this runtime by having the turn executed on the login machine.

## Consequences and open questions

- **Cold start**: a fresh CLI process costs ~2–4 s before the first token. The
  session is long-lived per agent incarnation, with `resume` as the restart story
  — not one process per turn.
- **Context ownership**: the SDK owns the transcript of a resumed session; the
  graph owns `state.messages`. The first wiring change must pick one owner
  (recommended: the SDK transcript, with the graph's window built from the
  bridge's events) or the two histories drift.
- **Tool surface**: `BridgeConfig.tools` is a hard allowlist wider than
  `execute_code`'s single tool; the system prompt and the allowlist change
  together.
- **Permission mode** is the CLI's own (`bypassPermissions` by default) because
  there is no bridge-level approval layer. A deployment that wants a narrower mode
  sets `permission_mode` and accepts that headless prompts are denied rather than
  answered.
- **Orphaned CLI on a hard kill**: `close()` reaps the CLI, but a supervisor killed
  with SIGKILL leaves the CLI reparented to init and still holding the session. The
  supervisor must sweep leftover `claude` children on agent restart, keyed by the
  session id in the registry, before starting a new session.
