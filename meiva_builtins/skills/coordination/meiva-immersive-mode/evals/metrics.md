# meiva-immersive-mode evaluation

Read the shared evaluation contract — `meiva.help(meiva.skills.meiva_guide)` →
`references/evaluation.md` — before running these cases. `evals.json` contains
constructed live-session and boundary scenarios; all scenario inputs are inline
in each prompt, and only writes under an isolated scratch directory are
permitted.

## Setup and baseline

Candidate: an agent with the `meiva-immersive-mode` skill available, in a
session the human started (cases 1, 3, 4, 5, 7, 8, 9) or did not start (cases 2,
6). Baseline: a snapshot of the prior skill revision (or a no-skill baseline for
trigger discovery comparisons). Hold the executor model, effort, tools, harness
type, and scratch layout fixed. Live execution requires an agent surface that
can act across turns and emit surface deltas and messages over time; where only
a single offline plan/response can be collected, grade the planned actions,
tool calls, and messages explicitly as an offline plan check rather than
claiming a live behavioral pass.

## Measures and acceptance

- **Observable-delta cadence & silence watchdog cap** (cases 1, 3, 5, 7, 8, 9):
  On active visual/audio/device surfaces (cases 7, 8), foreground tweaks drive
  an observable delta on the primary surface in seconds while chat stays a
  concise 1–2 sentence cue & control note (no long essays competing with the
  surface). Whenever the primary surface has no observable delta, every quiet
  window stays within the announced silence watchdog cap (default 60s) or is
  covered in advance by an explicit pre-notice naming the background step and
  next signal. Filler count is zero.
- **Live feedback rig priority** (cases 7, 8): When the primary surface is
  non-text, the agent stands up a minimal perceptual tracer rig first (`show`
  over an HMR/dev server or `serve` fallback while heavy setup runs in the
  background; a brief safe test tone/probe on audio/device surfaces) before
  iterating on adjustments.
- **Foreground `exec_timeout_seconds` lifecycle** (cases 1, 4, 5, 7, 8):
  - *Native Meiva harness* (cases 1, 4, 7, 8): On entry, the prior
    `exec_timeout_seconds` from the context note and the session contract are
    persisted to the workspace ledger *before* calling
    `meiva.self.restart(config_overlay={"exec_timeout_seconds": ...})` with a
    tightened bound (e.g., 25–30s, at most half the silence cap, and strictly
    less than `exec_node_timeout_seconds`). On exit (case 4), the saved value is
    restored via `meiva.self.restart(config_overlay=...)` after updating the
    ledger, with no duplicate close after the post-restart resume.
  - *Attached external CLI / impersonation harness* (case 5): `meiva.self.restart`
    is skipped cleanly without error, and hot/cold split is enforced by
    discipline.
- **Hot-path inline vs. cold-path delegation** (cases 1, 3, 5, 7, 8, 9): Fast,
  reversible surface deltas (targeted reads, small HMR CSS/parameter tweaks,
  1–2s audio probes) run inline on the foreground hot path; slow, multi-step, or
  noisy tasks (lint/test/typecheck suites, builds, installs, batch analyses,
  multi-service audits) are dispatched asynchronously before they can block the
  loop.
- **Two-tier sub-orchestration against wake storms** (case 9): When background
  work spans >2 concurrent tasks, the foreground spawns one background
  sub-orchestrator (or `meiva-dynamic-workflow` coordinator) rather than
  attaching leaf workers directly to the foreground; leaf workers report only to
  that sub-orchestrator, which relays only blockers or the aggregated result.
- **Human preemption latency** (case 3): A mid-session human question preempts
  and is answered within one small inline step without waiting on background
  jobs.
- **Contract & exit completeness** (cases 1, 4, 5, 7, 8): Entry names primary
  surface, silence watchdog cap, hot/cold split, timeout bound (when native),
  and exit rule; exit delivers handoff summary, ledger path, delegated owners,
  timeout restoration, and resumption of `reduce-context-switch-for-human`.
- **Task quality under tight pacing** (all session cases): Deliverables still
  receive full verification (e.g., background test/lint runs checked before
  claiming pass); fast surface cadence never substitutes for correctness.
- **Trigger & boundary discipline** (cases 2, 5, 6): Natural-language live
  requests activate the mode without needing literal `"immersive on"` (case 5);
  away/ordinary tasks (case 2) and explicit heads-down requests (case 6) stay in
  `reduce-context-switch-for-human` with zero unsolicited live behavior and at
  most one non-repeated offer when genuinely helpful.

Acceptance: every assertion passes across valid completed cases; blocked or
unexecuted cases leave acceptance incomplete. Elapsed time and token counts are
recorded as diagnostics when supplied by the runner, separate from quality
acceptance.

## Evidence status

- 2026-10-17 initial revision: initial 6 chat-focused cases and measures
  authored.
- 2026-10-31 shared-surface & kernel-timeout revision (`meiva-immersive-mode`):
  renamed package and upgraded measures and cases (1–9) to cover shared
  perceptual surfaces, live feedback rig setup, native-harness
  `exec_timeout_seconds` save/tighten/restore, hot-path inline tweaks paired
  with cold-path verification, attached-harness fallback, and sub-orchestrated
  multi-task fan-out. Live execution runs remain to be captured in an isolated
  evaluation environment.
