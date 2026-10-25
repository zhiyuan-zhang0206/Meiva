---
type: doc
title: ACP transport
description: '`base/lm/harness/acp/` — the provider-neutral ACP v1 stdio client: one child process, newline-delimited JSON-RPC in both directions, and the permission request an agent makes back to its controller.'
tags:
- base
- library
- external-agents
---

# ACP transport

The Agent Client Protocol is a controller protocol over one bidirectional
JSON-RPC stream. `AcpConnection` owns that stream and nothing else: the adapter
above it owns sessions, prompts and how a stop reason becomes a turn result.

## Shapes on the wire

| Frame | Meaning | Who answers |
|---|---|---|
| `id` + `method` | a request | the peer |
| `id` alone | the response to a request | the pending future |
| `method` alone | a notification | the notification stream |

`start()` spawns the child in its own process group and keeps two retained
tasks (stdout reader, stderr reader) inside a `TaskGroup`, so teardown is finite.

Stdout EOF closes protocol admission, but does not finish teardown: the child
can close its output while remaining alive, or its exit status can still await
the process watcher. `close()` serializes callers and waits for the owned child
and retained tasks even after EOF. Only a completed close may return early;
cancellation leaves the next caller able to finish teardown. The existing
shutdown and TERM/KILL budgets remain unchanged. Exhausting both signal waits
still leaves an unobserved status as `None`; completion does not invent an exit code.

## Three deliberate refusals

- **An unanswered permission request is `cancelled`.** The ACP contract lets an
  agent read that as a refusal; answering `allow` on the agent's behalf would
  turn a missing approval surface into an unaudited grant.
- **Every other client-side method answers `-32601`.** An agent that believes it
  read a file we never served will act on nothing and say nothing.
- **Teardown kills the process group, not just the pid.** A closed pipe leaves the
  tool children the agent spawned behind.

## Boundaries

No session vocabulary, no retries, no reconnect: a connection that closes is
gone, and the adapter above decides whether that is a faulted turn or a closed
session. Stdout is protocol only; a stray non-JSON line is recorded in
`stderr_tail` for the postmortem instead of killing the stream.
