---
type: doc
title: Harness — the runtime loop an agent runs on
description: spawn(harness=), the immutability and fork rules the SDK enforces, and harness_availability().
tags:
- agent-view
- sdk
- agent-lifecycle
---

# Harness

A harness is the shell that runs the agent loop — Meiva's own runtime, or an
external CLI such as Claude Code, Codex or Antigravity. The model and its
provider are separate choices. `Harness` values: `meiva` (default),
`claude_code`, `codex`, `antigravity`.

## Choosing one at spawn

`spawn(..., harness=None)`. Omitted, a plain spawn runs the built-in `meiva`
runtime and a fork keeps whatever harness its source was born on. An unknown
name is refused before any request is sent — there is no fallback to the
default. The value is written once, when the agent is born.

A harness no launcher can start is refused rather than recorded: `spawn` raises
`harness_not_launchable` (HTTP 400) and creates no agent,
and the detail names the harnesses that can start a session. A fork of a row born
on such a harness is refused the same way, because a fork keeps its source's
harness. `harness_availability()` answers a different question — whether *this
machine* is set up to run a harness — and a harness can be launchable by the
release while unavailable here.

## What cannot change it

Nothing. Restart and resurrect run the same harness, and any attempt to rewrite
the stored value is rejected rather than applied. Correcting a wrong value means
spawning a new agent.

A fork continues its source's identity, so it inherits the source's harness.
Naming the source's own value is accepted; naming a different one fails with
`fork_harness_mismatch` and creates nothing. To move onto another harness, spawn
a new agent instead of forking.

## Checking what this machine can run

`harness_availability() → list[HarnessAvailability]` returns one row per harness
for the machine the caller runs on:

- `harness`: which harness the row describes.
- `status`: `available` (installed and a credential marker is present),
  `unavailable` (not installed, or installed with a marker that holds no
  credentials), or `unknown` (installed, but the sign-in state cannot be read
  here). The probe does not validate credential freshness; an expired credential
  still reads as `available`.
- `reason`: one readable sentence explaining that status.
- `binary` and `on_path`: the resolved executable path this machine would run
  (following symlinks to the real target, which is not always the name on PATH),
  and whether PATH led to it. `on_path=false` means only a conventional install
  directory held it, so a process started with a trimmed PATH may not find it.

The check reads the filesystem only: it runs no command, starts no session and
spends no quota. A sign-in it cannot prove is reported as `unknown`, never as a
guess.

The stored column and the birth rule:
[[base/agents/docs/harness.meiva.okf.md|agent harness identity]].
