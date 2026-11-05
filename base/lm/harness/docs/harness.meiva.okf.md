---
type: doc
title: Provider harness
description: '`base/lm/harness/` — the provider-neutral half of the external-agent bridge: event vocabulary, the versioned handoff document, agent-id ↔ session-id registry, and the child-process probe that makes a cancel assertable.'
tags:
- base
- library
- llm-inference
- external-agents
---

# Provider harness

`base/lm/harness/` is what every external agent runtime shares with the host.
An adapter (Claude Agent SDK, Codex app-server, Antigravity CLI) owns the transport
and the message mapping; this package owns everything whose meaning
does not change when the provider does, so a second adapter adds a directory
instead of a second vocabulary.

The adapters implement one contract — `connect(resume)` / `send(text)` /
`interrupt()` / `stream()` / `close()` — and are expected to preserve nine
invariants the host relies on:

1. inbound is queued only at a turn boundary, never mid-turn;
2. a repeated submission key returns the same ticket, and a completed key
   replays its recorded outcome instead of re-running;
3. a full queue refuses; it never buffers beyond its bound;
4. `interrupt()` asserts that tool child processes were reaped — an unobservable
   process tree is reported as unknown, not as success;
5. turns have no default elapsed-time limit, including while the provider is
   quiet. A configured turn timeout interrupts; it never silently drops the turn.
   Setup, transport, cancellation and shutdown keep their own bounds;
6. usage and cost accumulate per turn, and `Usage.input_tokens` is the whole
   prompt — cache reads and cache writes included, with those two fields as
   subsets — whatever shape the provider reports them in;
7. the handoff is injected once, when the session is created, and never again on
   resume; when a bound session id is supplied (`resume` is non-`None` or loaded from
   the registry), a failed resume fails fast instead of silently
   falling back to a fresh session;
8. a faulted or closed session stops being usable: the bridge retires it instead of
   submitting to it again;
9. `close()` returns only after the session's process tree is gone, or raises when the
   adapter's settle window still observes residue. An unobservable tree remains
   explicitly unknown; it is not proof that every descendant exited.

Adapters: [[../claude/docs/claude.meiva.okf.md]], [[../codex/docs/codex.meiva.okf.md]],
[[../antigravity/docs/antigravity.meiva.okf.md]]. Contract, provider differences
and open questions are recorded in
[external harness adapters](../../../../docs/decisions/agents/graph/2026-10-19-external-harness.md).

## Modules

| Module | Owns |
|---|---|
| `base_session.py` | `BaseHarnessSession`, `TurnTicket`, `SessionBinding`, `InboundQueueFullError`, `SessionClosedError`, and `EVENT_STREAM_END`: the shared session state machine (inbound queue, turn promotion, idempotency dedupe, bounded event queue with delta dropping, turn timeout watchdog, background `TaskGroup` management, `SessionRegistry` persistence, injected `Clock`) |
| `cancel.py` | `CancelReport`, `ProcessObservation`, `describe`, `Sweep`, and the pure functions that cut a report from them |
| `events.py` | `BridgeEvent` and its members, plus the droppable (text delta) versus control split |
| `turn.py` | `SessionTotals`, `TurnOutcome`, `add_cost` |
| `engine.py` | `HarnessSession` (the adapter contract), `TurnEngine` (what a graph run needs), and `HarnessTurn` (one turn's reply, usage and boundary, plus the `prelude` turns that completed before it) |
| `handoff.py` | the six-section handoff document, `HANDOFF_VERSION`, and its rendered byte size |
| `registry.py` | `SessionRecord` and `FileSessionRegistry`: one JSON document per agent id, atomic replace |
| `registration.py` | `HarnessRegistration` and `RegistrationStore`: the attachment row the bridge writes, always with an expiry, plus the crash sweep |
| `process.py` | `ChildProbe`, the psutil implementation, tree snapshots and the bounded wait for a pid to disappear |

`process.py` stays provider-neutral on purpose: "is anything still running under
this pid?" is the same question for every adapter; only how an adapter learns its
root pid differs. `client_pid()` is the one best-effort helper: it reads an SDK
client's private transport, returning `None` rather than guessing. That debt is
registered in the [tech-debt ledger](../../../../future/tech-debt/ledger.md).

## CLI delivery and convergence

Session environment construction (`wiring.session_environ`), `PATH` rebuilding
(`with_harness_path`), `MEIVA_PYTHON` interpreter delivery, and per-adapter binary
resolution (`resolve_harness_binary`) are defined in
[[base/lm/harness/docs/cli-delivery.meiva.okf.md|Harness CLI and environment delivery]].
Path resolution, discovery, catalog pinning, host convergence steps, and runner update schedules are described in
[[base/lm/harness/docs/cli-convergence.meiva.okf.md|External harness CLI convergence and delivery]].

## The handoff document

The handoff is a wire format, not an internal dataclass: the waking agent reads
it as the first message of a fresh session, and its rendered size is a per-wake
cost the host reports (`Handoff.size_bytes()`, and a `HandoffInjected` event).
Section order, the versioned field set, the tests that pin both and
completed-key replay are defined in
[[base/lm/harness/docs/handoff.meiva.okf.md|The handoff document]].

## Who a message belongs to

The failure split, the `_claim` entry point and where unowned output goes:
[[base/lm/harness/docs/attribution.meiva.okf.md|attribution]].

## Identity

Session registry persistence, attachment claims and harness matching are defined in
[[base/lm/harness/docs/session-identity.meiva.okf.md|Harness session identity]].

## How a session ends

`close()` waits for the CLI child (and its tree) and then answers `exit_code`, in the
same three states the agent row carries: `None` while the child is running or was
never observable, `0` for a normal exit, non-zero for an abnormal one. The bridge
re-exposes it as `TurnEngine.exit_code` for the host that retires the session, and
only a close that finished is recorded — an agent on the built-in loop has no child
at all and stays `NULL` rather than record a fake `0` (task #5616).

## Testing

`base/lm/harness/tests/` covers the shared half alone — the handoff render and
its pinned field set, the registry's atomicity and refusal cases — with no
provider dependency, which is the point of the split.
