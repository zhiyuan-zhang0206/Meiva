---
type: doc
title: Harness — the runtime loop an agent runs on
description: Initial harness selection, restart switching, fork rules and harness availability in the SDK.
tags:
- agent-view
- sdk
- agent-lifecycle
---

# Harness

A harness is the shell that runs the agent loop — Meiva's own runtime, or an
external CLI such as Claude Code, Codex or Antigravity. The model and
its provider are separate choices. `Harness` values: `meiva` (default),
`claude_code`, `codex`, `antigravity`.

## Choosing one at spawn

`spawn(..., harness=None)`. Omitted, a plain spawn runs the built-in `meiva`
runtime. An unknown name is refused before any request is sent. Spawn sets the
initial harness.

A harness no launcher can start is refused rather than recorded: `spawn` raises
`harness_not_launchable` (HTTP 400) and creates no agent,
and the detail names the harnesses that can start a session. A fork of a row born
on such a harness is refused the same way, because a fork keeps its source's
harness. `harness_availability()` answers a different question — whether *this
machine* is set up to run a harness — and a harness can be launchable by the
release while unavailable here.

## Switching on restart and fork rules

An agent's harness can be switched bidirectionally (`meiva` <-> external, or between external harnesses) on restart:
`meiva.agents.restart(agent_id, message="...", config_overlay={"harness": "..."})`.
The agent must be in `idling` or `terminated` status, and a mandatory `message` explaining the switch is required.

For a switch requested during the target agent's own turn, save a handoff and
submit the restart from outside the target session after it becomes idle.
The transition cleans up agent-owned shell sessions, so a shell belonging to
the target is unsuitable for tracking its own restart result.

After a transition, the Inspector shows the configured model until the new
runtime reports model usage. Earlier usage stays available for history and
billing.

`meiva.agents.resurrect(agent_id, prompt)` wakes a terminated agent on its current
harness.

Forking is supported only on the native `meiva` harness. Forking an agent
currently on an external harness or requesting an external harness for a fork
fails with `fork_harness_mismatch`. A fork can switch harness after creation.

## Checking what this machine can run

`harness_availability() → list[HarnessAvailability]` returns one row per harness
for the machine the caller runs on:

- `harness`: which harness the row describes.
- `status`: `available` (installed, signed in, and live model options read),
  `unavailable` (not installed, no sign-in, or credentials that cannot renew and
  have expired), or `unknown` (installed, but login state or live model options
  could not be read here — an account recorded in a config file is not a login).
- `reason`: one readable sentence explaining that status.
- `binary` and `on_path`: the executable this machine would start (the path
  PATH resolved, which is the symlink itself when there is one, not its target),
  and whether PATH led to it. `on_path=false` means only a conventional install
  directory held it, so a process started with a trimmed PATH may not find it.
- `models` and `default_model`: live `ProbedHarnessModel` entries (`id`,
  `reasoning_effort_options`, `reasoning_effort_default`) and the CLI's default
  model when `status="available"` on an external harness (empty for `meiva`,
  which uses the shared model catalog).

The check asks the CLI for its login state under a bounded timeout and falls back
to the stored credential when the CLI cannot answer: it starts no session and
spends no quota. A sign-in it cannot prove is reported as `unknown`, never as a
guess.

The stored column and the birth rule:
[[base/agents/docs/harness/harness.meiva.okf.md|agent harness identity]].
