---
type: doc
title: Codex execution policy — sandbox and approvals
description: 'Why the codex child runs with `danger-full-access` and `never` approvals on Ubuntu 24.04, and the request/answer table the bridge uses when a policy makes the server ask.'
tags:
- base
- library
- llm-inference
- codex
---

# Codex execution policy

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

Parent: [[base/lm/harness/codex/docs/codex.meiva.okf.md|Codex app-server session bridge]].
