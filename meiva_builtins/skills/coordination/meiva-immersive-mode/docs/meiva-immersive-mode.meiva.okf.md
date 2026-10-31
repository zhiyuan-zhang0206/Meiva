---
type: doc
title: meiva-immersive-mode skill — tight-loop live sessions with the human present
description: An opt-in, human-triggered live-session discipline across a shared perceptual surface — live feedback rig first, hot/cold execution split, foreground exec_timeout_seconds tightening/restoration, two-tier sub-orchestration, durable ledger, and clean exit back to batch reporting.
tags: [extensions, agent-instruction]
---

# meiva-immersive-mode skill — tight-loop live sessions with the human present

## What it is

`meiva-immersive-mode` runs a tight-loop live session when the human is present
and actively steering. Rather than assuming all live collaboration is chat text,
it anchors pacing on a **shared perceptual surface** — conversation, a live web
or app preview registered through [[meiva/docs/ui.meiva.okf.md|meiva.ui]]
(`show` for an HMR/dev server, `serve` for a static/prototype directory), or a
physical/multimodal output such as audio tones or GUI/device actions.

On non-text surfaces, the agent stands up a minimal **live feedback rig first**
(dispatching slow initial builds to the background while aligning or serving a
lightweight prototype if needed). Surface-level **observable deltas** close in
seconds with chat reduced to brief **cue & control** notes, while a **silence
watchdog cap** (default 60s) guards against unannounced stalls when no surface
delta is occurring.

Foreground execution stays on the **hot path** — conversation, targeted single
reads, small hot-reload/parameter tweaks on an active rig, and brief device
probes — while builds, installs, multi-file edits, test/lint suites, and broad
research go to the **cold path** (`meiva.agents.spawn`,
`meiva.shell.run_background`, [[meiva/watcher/docs/watcher.meiva.okf.md|meiva.watcher]]).
On the native Meiva harness, entry records the prior `exec_timeout_seconds` in a
durable workspace ledger and tightens the single-call ceiling via
[[meiva/self/docs/self.meiva.okf.md|meiva.self.restart]] (`config_overlay`),
restoring the saved value on exit. When background work fans out beyond two
concurrent tasks, a single background sub-orchestrator (or
[[meiva_builtins/skills/coordination/meiva-dynamic-workflow/docs/meiva-dynamic-workflow.meiva.okf.md|meiva-dynamic-workflow]]
script) aggregates leaf-worker traffic so raw `send_message` wakeups do not
flood the live foreground.

## Relationship to the default reporting discipline

The default ([[meiva_builtins/plugins/meiva_fleet/skills/reduce-context-switch-for-human/docs/reduce-context-switch-for-human.meiva.okf.md|reduce-context-switch-for-human]])
protects the human's attention while they are away: results queue, updates
batch, and nothing depends on live presence. `meiva-immersive-mode` is the
complementary regime while the human chooses to be present on a shared surface.
Both regimes run heavy engineering work asynchronously; they differ in surface
cadence, foreground execution bounds, and when signals reach the human.

## Boundaries

- **Human-triggered entry only**: The agent may offer a live pass at most once
  when real-time steering helps, and never starts or extends a session on its
  own (2026-10-17 user ruling).
- **Session-scoped and reversible**: Scoped to the active foreground session;
  explicit exit hands off via the workspace ledger, restores
  `exec_timeout_seconds` on the native harness, and resumes default batch
  reporting.
- **Harness boundary**: Attached external CLI and impersonation harnesses where
  `meiva.self.restart` is refused skip `config_overlay` timeout changes and rely
  on hot/cold execution discipline.
- **Wake-storm mitigation**: Platform routing has no per-session do-not-disturb
  primitive yet; the foreground agent shields the loop via two-tier
  sub-orchestration (>2 concurrent tasks) and local batching of non-urgent fleet
  messages.

## Validation

Cases live in `evals/evals.json` and measures in `evals/metrics.md`, covering
entry/exit timeout lifecycle, non-text perceptual surfaces (HMR UI preview and
audio/device probes), human preemption, sub-orchestrator fan-out, and positive
and negative trigger boundaries.

[[meiva_builtins/skills/docs/skills.meiva.okf.md|Skills index]]
