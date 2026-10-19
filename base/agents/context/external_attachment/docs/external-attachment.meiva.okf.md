---
type: doc
title: External attachment kernel
description: '`base/agents/context/external_attachment/` — the borrowed-identity kernel both a takeover lease and a provider harness attach through: boot, state replay, admission gate, flush and detach ordering.'
tags:
- base
- agents
- external-agents
---

# External attachment kernel

An external process acting as a Meiva agent binds one borrowed identity into its
own Python process. Two identities exist today: a **takeover lease** (a controller
acting as an agent) and a **harness registration** (a provider runtime acting as
an agent). They differ in how the identity is proved and where its plugin-state
delta lives; they do not differ in what must happen around it.

This package owns that shared half:

| Step | What it does | Why it matters |
|---|---|---|
| refuse | a host runtime, an already-attached process and an exec turn are refused | one process, one borrowed identity |
| boot | config, machine, database and client set | an independent attachment owns its clients; a nested one reuses the bound context |
| replay | load the agent snapshot, apply the identity's plugin deltas under `applied_version` | a takeover continues native plugin state instead of resetting it |
| admit | every SDK call passes one gate that revalidates the identity | an expired lease or a closed registration stops the process at its next action |
| flush | write the delta back under optimistic version control | two attachments cannot silently overwrite each other |
| close | close admission, seal, deliver telemetry, detach, restore the prior context | a later cleanup error never replaces the first failure; an attachment that reused a bound context's clients owns no producer and delivers nothing |

## Identity sources

`IdentitySource` is the whole extension point: `require`, `validate`, `machine`,
`agent_id`, `delta_version`, `merge_delta`. Implementations:

- `meiva/impersonation/attachment.py` — the lease row, derived from
  (agent, session, machine) and rechecked on every call.
- `meiva/impersonation/harness/attach.py` — a harness registration handle (see
  [[meiva/impersonation/harness/docs/harness-attach.meiva.okf.md]] for the handle and what it
  proves).

## Failure behaviour

A refusal, an expired identity or a version conflict raises at the call site; the
kernel never degrades to "an attachment that writes anyway". `close()` runs the
same three cleanups in the same order for every identity, and preserves the first
exception while attaching later ones as notes.
