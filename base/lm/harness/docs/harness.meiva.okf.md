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
6. usage and cost accumulate per turn, and `Usage.input_tokens` is the whole
   prompt — cache reads and cache writes included, with those two fields as
   subsets — whatever shape the provider reports them in;
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

## Where the CLI comes from

A harness session starts from the daemon child environment, which is a positive
allowlist and therefore carries no `PATH`: a bare `codex`/`claude`/`agy` would
resolve against the OS default (`/usr/bin:/bin:/usr/sbin:/sbin`), so a real spawn
failed while `which codex` in a shell succeeded. Two rules close that gap.

`base/host/env/harness_binaries.py::with_harness_path` rebuilds `PATH` at the
session delivery point (`wiring.session_environ`): the host's own entries first,
then the directories harness CLIs install into (`~/.local/bin`, `~/.npm-global/bin`,
`~/.bun/bin`, `~/.claude/local`, `/usr/local/bin`, `/opt/homebrew/bin`), then the
system dirs. The same list feeds the probe in `base/agents/birth/harness.py` and the
`launchd` job PATH in `base/host/system/cron.py`, so all three agree on where the
CLIs live.

Every unit this repository installs composes its PATH through
`harness_binaries.harness_unit_path`, which appends the table and then refuses a
result that omits any of it (`HarnessPathIncompleteError`, naming the unit and the
directories): a hand-written narrow PATH fails where the unit is written, not at the
first spawn.

Each adapter then spawns the path it resolved, not the name: `codex` and `agy` cache
the resolution, and the Claude Code session passes the SDK a `cli_path` because the
SDK resolves a bare name with `shutil.which` against its own process environment. A
failure names the PATH and every directory that was searched instead of a bare
`No such file or directory`, and a configured absolute path is never redirected.

## The handoff document

The handoff is a wire format, not an internal dataclass: the waking agent reads
it as the first message of a fresh session, and its rendered size is a per-wake
cost the host reports (`Handoff.size_bytes()`, and a `HandoffInjected` event).
Section order, the versioned field set and the tests that pin both:
[[handoff.meiva.okf.md]].

## Identity

`FileSessionRegistry` owns `agent_id ↔ session_id`; a registration is not a
second identity but the bridge's claim that a session belongs to one agent, and
three facts gate it. Records, expiry and the self-describing `harness` field:
[[identity.meiva.okf.md]].

## Testing

`base/lm/harness/tests/` covers the shared half alone — the handoff render and
its pinned field set, the registry's atomicity and refusal cases — with no
provider dependency, which is the point of the split.
