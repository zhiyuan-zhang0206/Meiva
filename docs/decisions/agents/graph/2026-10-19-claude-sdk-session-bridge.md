# Wiring the Claude Agent SDK session into the turn model

## Context

`base/lm/claude_sdk` (v0.1) drives one Claude Agent SDK session: start / resume /
close, one inbound message per turn, an event stream, approvals, interrupts and
cost accounting. It is deliberately not connected to the agent loop yet. This
note is the design for connecting it, so the wiring change is a mechanical edit
rather than a redesign.

The spike (`~/.meiva/workspaces/6982/spike/REPORT.md`, 2026-10-19) established the
facts the design rests on: the SDK runs on a subscription login without an API
key; `session_id` resumes across processes; `can_use_tool` is a synchronous await
point before the tool runs; `interrupt()` returns in ~5ms and the CLI reaps the
tool's child processes itself (~40ms observed); and a `query()` issued mid-turn is
folded into the pending tool result, where the model treats it as injected
content and refuses it.

## Decision

The bridge replaces the *model call*, not the graph. `agent/graph/llm` keeps
owning the turn's shape; only the body of one LLM round changes.

### How a message gets in

The claim node already owns the durable inbound queue. It keeps that role and
gains exactly one new step: instead of writing the batch into `state.messages`
and letting the LLM node call a chat model, it hands the batch's user text to
`ClaudeSession.submit(text, key=inbound_row_id)`.

- **One claim batch = one bridge turn.** The `ResultMessage` is the turn
  boundary, exactly as the graph's `BEFORE_LLM → AFTER_EXEC` cycle is today.
- **The idempotency key is the inbound row id**, so a gateway redelivery inside
  `dedupe_window` replays the recorded outcome instead of running the turn twice.
- **Backpressure stays where the durable copy is**: `InboundQueueFull` means the
  claim node leaves the row unclaimed and retries, never that a message is lost.
- The claim node does **not** submit while a bridge turn is running. It waits for
  the boundary, which is the same condition it uses today to start a new round.

### How output leaves

A single consumer task drains `session.events()` for the life of the session and
maps each event onto what the graph already produces:

| Bridge event | Graph / UI destination |
|---|---|
| `TextDelta` | the existing `RedisStreamHandler` token stream (SSE) |
| `AssistantText` | the assistant message body for the round |
| `ToolUse` / `ToolResult` | the tool message + timeline event `execute_code` produces today |
| `TurnCompleted` | the round's final `AIMessage` (`text`), plus usage/cost |
| `RateLimitObserved` | the usage signal that backs the quota warning |
| `SessionStarted` | binds `session_id` to the agent incarnation |
| `SessionFaulted` | the llm node's retry path |

Because the SDK's `execute_code` equivalent is a *real* tool call rather than a
Python namespace, the wiring maps one SDK `ToolUse`/`ToolResult` pair onto the
existing exec-node message pair. The agent's system prompt and tool allowlist
(`BridgeConfig.tools`) must then name the same capability set the exec node
offers today — otherwise the brain and the hands disagree.

### How approvals map

`can_use_tool` is already a host callback, so the mapping is one function:

- The bridge publishes `ApprovalRequested` (with `ctx.tool_use_id` as its id) into
  the session's event stream.
- The graph's approval surface listens for it and writes the same pending row it
  writes for a `meiva.*` approval today; the human answer calls
  `session.resolve_approval(id, allow=..., message=...)`.
- `BridgeConfig.allowed_tools` is the auto-allow list (read-only tools). A policy
  callback may decide without a human; the timeout always denies.

The invariant worth keeping: an approval is never a silent hang. `ApprovalBroker`
bounds every wait, and a timeout produces a tool result the model can read.

### How cancel is sent

Today's native cancel path (`agent/ownership/native_cancel.py`) already owns the
lifecycle decision; the bridge gets one call from it:

```python
report = await session.interrupt()
if report.settled is not True:
    # residue, or an unobservable CLI — surface it, never assume success
```

`report.settled is None` (no observable CLI pid) is *not* a pass; the wiring must
treat it as unknown and record it. This is the point that impersonation used to
get wrong, so the assertion lives in the bridge rather than in a caller's memory.

### How cost is recorded

`TurnCompleted` carries the SDK's per-turn `usage` and `total_cost_usd`;
`SessionTotals` carries the running sum. The wiring writes the same `llm_usage`
row the LangChain path writes (`base/lm/usage.py`), with:

- `usage_kind="agent"` and `source="claude_sdk"` so the two runtimes stay
  comparable in the inspector;
- the subscription caveat recorded once per session: under OAuth, `total_cost_usd`
  is the **list price**, not money paid. The real budget signal is
  `RateLimitObserved` (`utilization`, `resets_at`), which the wiring should feed
  to the quota monitor instead of adding dollars.

### Where it runs

Whichever machine holds the Claude subscription login — today macmini. This is a
**credential constraint, not a capacity constraint**: the executing machine is
chosen by where `auth status` reports `authMethod: claude.ai`. An agent on another
machine reaches this runtime by having the turn executed on the login machine,
the same way impersonation already routes work by machine.

## Consequences and open questions

- **Cold start**: a fresh CLI process costs ~2–4s before the first token. The
  session is meant to be long-lived per agent incarnation (start once, many
  turns), with `resume` as the restart story — not one process per turn.
- **Context ownership**: the SDK owns the conversation transcript for a resumed
  session; the graph owns `state.messages`. The first wiring change must decide
  one owner (recommended: the SDK transcript, with the graph's window built from
  the bridge's events) or the two histories will drift.
- **Tool surface**: `BridgeConfig.tools` is a hard allowlist. The default set
  (`Read/Grep/Glob/Edit/Write/Bash`) is wider than `execute_code`'s single tool,
  so the system prompt and the allowlist must be changed together.
- **Orphaned CLI on a hard kill**: `close()` reaps the CLI, but a supervisor
  killed with SIGKILL leaves the CLI reparented to init and still holding the
  session (observed once during the demo: the CLI survived its parent). The
  wiring's supervisor must sweep leftover `claude` children on agent restart,
  keyed by the session id it recorded, before starting a new session.
- **Rate-limit behaviour** is observed, not enforced: nothing in the bridge pauses
  on `rejected`. Add a policy at the claim node (defer the turn) rather than a
  sleep inside the session.
