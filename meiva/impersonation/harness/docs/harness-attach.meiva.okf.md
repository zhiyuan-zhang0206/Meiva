---
type: doc
title: Harness attach
description: '`meiva/impersonation/harness/` — the seam that lets a provider harness process (Claude Code, Codex, Antigravity, dsh) act as a Meiva agent: a registration row, a hashed handle delivered through the environment, and the attach step that binds the borrowed identity.'
tags:
- meiva
- harness
- external-agents
---

# Harness attach

A provider harness runs the agent loop in its own process and cannot use the
handoff document alone: `import meiva` in a bare interpreter fails, because every
`meiva.*` call needs an initialized process pipeline. `meiva.impersonation.harness.attach()`
gives that process a borrowed identity, the same way `meiva.impersonation.attach()`
gives one to a takeover; both go through
[[base/agents/context/external_attachment/docs/external-attachment.meiva.okf.md]].

| Step | What it does |
|---|---|
| refuse | a host runtime, an already-attached process and an exec turn are refused |
| look up | the handle resolves to one registration row; the plain value never touches disk |
| boot | config, machine, database, client set and clock come from `meiva/boot.py` |
| prove | the row is attached and unexpired, the machine matches, the caller descends from the controller pid |
| bind | a borrowed `ExternalLease` on the ambient context, pipeline and SDK state initialized |
| close | flush the plugin delta, seal events, deliver telemetry, detach, retire the row |

## The registration row

The bridge writes one row per started session
(`base/lm/harness/` adapters own the writing; `FileRegistrationStore` owns the
file format under `$MEIVA_HOME/harness/registrations/`). The fields are the
agent id, the harness name, the provider-side session id, the machine, the
controller pid, the handle digest, the creation time, and an optional expiry and
status.

**The handle** is an opaque `hx_<random>` string. Only its SHA-256 is stored, so a
leaked registration file does not yield a usable handle. The bridge hands the
plain value to the child through `MEIVA_HARNESS_HANDLE`, deliberately **not** as a
workspace file: a provider sandbox reads everything and writes only its workspace,
so a file would hand the token to the very agent it authorises.

## What admission checks

Every SDK call passes `admit_sdk_call`, which refuses a closed attachment and then
revalidates the identity: the row is still attached and unexpired, the process
runs on the row's machine, and the process descends from the controller pid
recorded at start. A failed check raises `RegistrationError` at the call site; the
attachment never degrades into one that writes anyway.

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
