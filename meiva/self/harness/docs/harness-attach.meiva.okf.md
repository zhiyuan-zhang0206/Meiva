---
type: doc
title: Harness attach
description: '`meiva/self/harness/` — the seam that lets a provider harness process (Claude Code, Codex, Antigravity, dsh) act as a Meiva agent: a registration row, a hashed handle delivered through the environment, and the attach step that binds the borrowed identity.'
tags:
- meiva
- harness
- external-agents
---

# Harness attach

A provider harness runs the agent loop in its own process and cannot use the
handoff document alone: `import meiva` in a bare interpreter fails, because every
`meiva.*` call needs an initialized process pipeline. `meiva.attach()` gives that process a borrowed identity, the same way
`meiva.impersonation.attach()` gives one to a takeover; both go through
[[base/agents/context/external_attachment/docs/external-attachment.meiva.okf.md]].

| Step | What it does |
|---|---|
| refuse | a host runtime, an already-attached process and an exec turn are refused |
| look up | the handle resolves to one registration row; the plain value never touches disk |
| boot | config, machine, database, client set and clock come from `meiva/boot.py` |
| prove | the row is attached and unexpired, the machine matches, the caller descends from the controller pid |
| bind | a borrowed `ExternalLease` on the ambient context, pipeline and SDK state initialized |
| close | flush the plugin delta, seal events, deliver telemetry, detach |

`meiva.attach()` is the public entry: with no argument it reads
`MEIVA_HARNESS_HANDLE`, attaches once and returns the attachment. Every session's
first identity read does the same thing, so the call is optional.

`meiva harness exec --file script.py` is the CLI around the same seam: it reads the
script (or stdin as `-`) and runs it inside the attachment, so a bridge needs no
bootstrap file in the child's workspace.

## The registration row

**No writer lives in this tree.** This package reads and validates a registration
row; nothing here writes one, so a machine without the wiring piece answers
`attach()` with `no harness session matches this handle`. The writer is the
bridge's half of the seam: it creates one row per started session, hands the
handle to the child, and retires the row when the session ends. Until that half
is connected, the end-to-end path is only as complete as the test that fabricates
a row itself.

`FileRegistrationStore` owns the file format under
`$MEIVA_HOME/harness/registrations/`: the agent id, the harness name, the
provider-side session id, the machine, the controller pid, the handle digest, the
creation time, and an optional expiry and status. The bridge owns the row; the
child never writes it back — a provider sandbox allows writes only inside the
workspace, so a closing child could not delete the row anyway, and a failed
cleanup must not turn a finished turn into an error.

Two constraints the writer must honour, both about the window in which a stale row
still admits a caller:

- `expires_at` must not outlive the session it belongs to. It is optional in the
  format, so an expiry-less row stays valid until the bridge retires it; the
  ancestry check is the only remaining guard, and it accepts the recorded
  controller pid itself, which a reused pid would satisfy.
- The row names a controller pid, not a start time; keeping the session short and
  the expiry no later than it keeps a recycled pid from reviving the authority.

**The handle** is an opaque `hx_<random>` string. Only its SHA-256 is stored, so a
leaked registration file does not yield a usable handle. The bridge hands the
plain value to the child through `MEIVA_HARNESS_HANDLE`, deliberately **not** as a
workspace file: a provider sandbox reads everything and writes only its workspace,
so a file would hand the token to the very agent it authorises.

## Reaching the SDK from inside the child

The bridge cannot run `attach()` inside the provider's process, and the handoff
document cannot do it either, so the SDK makes the attachment itself — on the
**first read that needs an identity**, never at `import meiva`:

| Read | Path it passes through |
|---|---|
| `meiva.context`, `meiva.DB`, `meiva.REDIS` | the context slot |
| `meiva.self.AGENT_ID`, every `meiva.agents` call | `agent_identity._bound()` |
| a plugin namespace (`meiva.tasks`, …) | `boot.maybe_load_plugins_for_missing` |

Attaching replays the agent's plugin-state snapshot through the codec, and that
opens the database. Doing it at import would make every provider tool call that
merely imports the SDK open a connection — a probe, a syntax check, a turn with
nothing to record. Importing `meiva` therefore stays what it is in every other
process, and the handle decides nothing until an identity is asked for, which
also keeps the SDK's import budget (#3816) intact.

File and shell calls reach the same read when they resolve a path against the
agent's workspace, and that is correct: a relative path belongs to that
workspace, not to the operator's `$HOME`. What they do *not* do is fail when the
borrow fails — see below.

A failed attachment is recorded, not raised:

- calls that answer without an identity keep working — an absolute-path file
  read, `meiva.ui` — and `meiva.files` / `meiva.shell` fall back to `$HOME`, the
  documented pre-bootstrap base, instead of failing. `HarnessAttachError` is a
  `ContextOutsideProcessError`, which is what makes those readers keep their own
  fallback while the reads that need an identity report the reason;
- whatever the failed attach had already installed is rolled back, so no
  half-built surface answers for an identity that is not there;
- the next read that needs an identity raises `boot.HarnessAttachError` carrying
  the row's own reason (no matching handle, expired, another machine) and the
  instruction to stop retrying and report it instead of working around it.

The reason is kept in the install slot (`mark_load_failed`), the SDK's existing
"attempted and failed, do not retry" memory, so a dead session is not re-probed
on every attribute read. Two concurrent first reads cannot build two attachments
either: `ExternalAttachment.open` holds a process-wide non-blocking lock and
refuses the second. `ExternalAttachment.close()` runs from `atexit`: it flushes
the turn's events, seals the participant, delivers telemetry, and only then
detaches.

## What admission checks

Every SDK call passes `admit_sdk_call`, which refuses a closed attachment and then
revalidates the identity: the row is still attached and unexpired, the process
runs on the row's machine, and the process descends from the controller pid
recorded at start. A failed check raises `RegistrationError` at the call site; the
attachment never degrades into one that writes anyway.

**Plugin-state deltas are not persisted by this identity.** The harness does not
write plugin state back: `merge_delta` records nothing, so a flush succeeds and
changes nothing. A harness agent therefore starts from the snapshot the row's
agent already has and leaves it untouched — deliberate, because the bridge owns
the agent's durable state and two writers would race.

**There is no capability gate.** A stored declaration is a record, not a check
(user ruling, 2026-10-19), so an attached harness agent has the same rights as a
native one. Lifecycle self-calls — `compact`, `restart`, `terminate` — belong to
the bridge by convention, and the SDK refuses them on its own ground:
`assert_self_action` requires the caller to own the agent's turn loop, which an
external process never does.

## Why not MCP, and why not a sidecar

MCP would add a process, a tool description and a second interaction model for a
capability the harness already has through the SDK; a sidecar bridge adds a
service, an auth scheme and its own version surface. The kernel gives the harness
the calls a native agent makes, with the same telemetry, from one implementation
with two entry points.
