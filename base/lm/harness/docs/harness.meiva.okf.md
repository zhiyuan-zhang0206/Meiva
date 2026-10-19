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
An adapter (Claude Agent SDK, DeepSeek Harness over ACP, Codex app-server, Antigravity CLI) owns the
transport and the message mapping; this package owns everything whose meaning
does not change when the provider does, so a second adapter adds a directory
instead of a second vocabulary.

The adapters implement one contract — `connect(resume)` / `send(text)` /
`interrupt()` / `stream()` / `close()` — and are expected to preserve seven
invariants the host relies on:

1. inbound is queued only at a turn boundary, never mid-turn;
2. a repeated submission key returns the same ticket, and a completed key
   replays its recorded outcome instead of re-running;
3. a full queue refuses; it never buffers beyond its bound;
4. `interrupt()` asserts that tool child processes were reaped — an unobservable
   process tree is reported as unknown, not as success;
5. a turn timeout interrupts; it never silently drops the turn;
6. usage and cost accumulate per turn;
7. the handoff is injected once, when the session is created, and never again on
   resume;
8. a faulted or closed session stops being usable: the bridge retires it instead of
   submitting to it again;
9. `close()` returns only after the session's process tree is gone, or after the
   adapter's settle window proved it is not — residue is reported, never assumed.

Adapters: [[../claude/docs/claude.meiva.okf.md]],
[[../deepseek/docs/deepseek.meiva.okf.md]]. Contract, provider differences
and open questions are recorded in
[external harness adapters](../../../../docs/decisions/agents/graph/2026-10-19-external-harness.md).

## Modules

| Module | Owns |
|---|---|
| `events.py` | `BridgeEvent` and its members, plus the droppable (text delta) versus control split |
| `engine.py` | `HarnessSession` (the adapter contract), `TurnEngine` (what a graph run needs), and `HarnessTurn` (one turn's reply, usage and boundary, plus the `prelude` turns that completed before it) |
| `handoff.py` | the six-section handoff document, `HANDOFF_VERSION`, and its rendered byte size |
| `registry.py` | `SessionRecord` and `FileSessionRegistry`: one JSON document per agent id, atomic replace |
| `registration.py` | `HarnessRegistration` and `RegistrationStore`: the attachment row the bridge writes, always with an expiry, plus the crash sweep |
| `process.py` | `ChildProbe`, the psutil implementation, tree snapshots and the bounded wait for a pid to disappear |

`process.py` stays provider-neutral on purpose: the question it answers — "is
anything still running under this pid?" — is the same for every adapter, and only
the way an adapter learns its root pid differs. `client_pid()` is the one
best-effort helper: it reads an SDK client's private transport and returns `None`
rather than guessing when the shape changes. That debt is registered in the
[tech-debt ledger](../../../../future/tech-debt/ledger.md).

## The handoff document

The handoff is a wire format, not an internal dataclass: the waking agent reads
it as the first message of a fresh session, and its rendered size is a per-wake
cost the host reports (`Handoff.size_bytes()`, and a `HandoffInjected` event).

Section order is fixed — identity, cluster, entry points, do not use, reporting,
task — because an agent that skims it still meets the task last. `HANDOFF_VERSION`
is part of the document (`- handoff version: 1` in section 1) so a provider that
caches or replays a handoff can tell which field set it holds.

Section 3 names where to start, not an inventory: `DEFAULT_ENTRIES` holds the
`meiva` entry — which attaches this session's identity on first use — plus the
three namespaces a turn reaches for first (files, tasks, agents). Listing every
namespace there would become a second copy of `meiva.help()` that drifts from the
SDK; the entry point and its pointer are enough to start.

Changing the field set or the section order means bumping the version.
`tests/test_handoff.py` pins both: the field tuple is compared against
`PINNED_HANDOFF_FIELDS` and the version against a literal, so the edit that
changes the shape fails until the version moves with it.

## Identity

`FileSessionRegistry` writes `agent_id ↔ session_id` (plus cwd, model, resume
count and the harness that wrote it) as soon as the adapter reports the session
started. Writes are a temp file
plus `Path.replace`, so a reader never sees a half-written record, and a corrupt
document raises instead of silently starting a fresh session under an id that
already has history.

A registration is not a second identity: it is the bridge's claim that a session on
this machine belongs to one agent. Three facts gate it — the row says `attached`,
`expires_at` has not passed, and the calling process descends from
`controller_pid` — and the expiry is the one the bridge cannot forget, because the
others degrade silently: a pid gets reused, and a row that never expires would be a
bearer capability nobody can revoke. The bridge rewrites the row at the start of
every turn, so the deadline only runs out when the writing host is gone.

`SessionRecord.harness` makes the registry self-describing. A record written
before the field existed reads as `meiva` — the only harness that predates it —
and every write names its harness, so the fallback can only describe an older
document. `load(agent_id, harness=...)` refuses a record that names another
harness instead of resuming one provider's transcript under another provider's
session, and the wiring passes its harness through a scoped registry rather than
trusting every caller to remember.

## Testing

`base/lm/harness/tests/` covers the shared half alone — the handoff render and
its pinned field set, the registry's atomicity and refusal cases — with no
provider dependency, which is the point of the split.
