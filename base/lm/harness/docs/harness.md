# Provider harness

`base/lm/harness/` is what every external agent runtime shares with the host.
An adapter (Claude Agent SDK, Codex app-server, Antigravity CLI) owns the transport
and the message mapping; this package owns everything whose meaning
does not change when the provider does, so a second adapter adds a directory
instead of a second vocabulary.

The adapters implement one contract — `connect(resume)` / `send(text)` /
`interrupt()` / `stream()` / `close()` — and are expected to preserve nine
invariants the host relies on:

1. an input is handed over with `deliver()` and reported only by events; the session
   keeps no queue, and the caller decides what to hand over next;
2. an input's key is the caller's own and is echoed by every event about it; a
   repeated key is a re-delivery that reaches the harness, never a replayed answer;
3. nothing is buffered beyond the bounded event queue;
4. a hosted `interrupt()` targets the running turn, whoever began it, and preserves the
   session. A matching provider terminal result confirms cancellation. Process
   observations report cleanup separately. Providers without a foreground-only
   control leave the turn running and report that cancellation is unavailable;
5. turns can run for as long as the provider needs, including while it is quiet.
   Setup, transport, cancellation and shutdown keep their own bounds;
6. usage and cost accumulate per turn, and `Usage.input_tokens` is the whole
   prompt — cache reads and cache writes included, with those two fields as
   subsets — whatever shape the provider reports them in;
7. the handoff is injected once, when the session is created, and never again on
   resume; when a bound session id is supplied (`resume` is non-`None` or loaded from
   the registry), a failed resume fails fast instead of silently
   falling back to a fresh session;
8. a faulted or closed session stops being usable: the bridge retires it instead of
   delivering to it again;
9. `close()` returns only after the session's process tree is gone, or raises when the
   adapter's settle window still observes residue. An unobservable tree remains
   explicitly unknown; it is not proof that every descendant exited.

Adapters: [Claude harness adapter](../claude/docs/claude.md), [Codex app-server session bridge](../codex/docs/codex.md),
[Antigravity (agy) session bridge](../antigravity/docs/antigravity.md). Contract, provider differences
and open questions are recorded in
[external harness adapters](../../../../docs/decisions/agents/graph/2026-10-19-external-harness.md).

## Modules

| Module | Owns |
|---|---|
| `base_session.py` | `BaseHarnessSession`, `ActiveTurn` and `SessionBinding`: the shared session state (the turn an input of ours started in an idle session, the delivery ledger, bounded event queue with delta dropping, background `TaskGroup` management, `SessionRegistry` persistence, injected `Clock`). There is no queue and no outcome: a turn is reported only by its events, and what to deliver next is the caller's decision |
| `cancel.py` | `CancelReport`, `ProcessObservation`, `describe`, `Sweep`, and the pure functions that cut a report from them |
| `events.py` | `BridgeEvent` and its members, plus the droppable (text delta) versus control split |
| `turn.py` | `SessionTotals`, `add_cost` |
| `engine.py` | `HarnessSession` (the adapter contract), `TurnEngine` (what a graph run needs), and `HarnessTurn` (one turn's reply, usage and boundary) |
| `handoff.py` | the six-section handoff document, `HANDOFF_VERSION`, its rendered byte size, and the workspace file it is saved to for after a compaction |
| `registry.py` | `SessionRecord` and `FileSessionRegistry`: one JSON document per (agent id, harness), atomic replace; `migrate_single_slot_records` converts the older one-document-per-agent layout; the source of a fork's session to clone ([fork](fork.md)) |
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
[Harness CLI and environment delivery](cli-delivery.md).
Path resolution, discovery, catalog pinning, host convergence steps, and runner update schedules are described in
[External harness CLI convergence and delivery](cli-convergence.md).

## The handoff document

The handoff is a wire format, not an internal dataclass: the waking agent reads
it as the first message of a fresh session, and its rendered size is a per-wake
cost the host reports (`Handoff.size_bytes()`).
Section order, the versioned field set and the tests that pin both are defined in
[The handoff document](handoff.md).

## Who a message belongs to

The failure split, the `_claim` entry point and where unowned output goes:
[attribution](attribution.md).

## Input delivery and acknowledgements

One `deliver()` operation, two acknowledgements, observed turns and reconciliation:
[delivery](delivery.md).

## Identity

Session registry persistence, attachment claims and harness matching are defined in
[Harness session identity](session-identity.md).

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
