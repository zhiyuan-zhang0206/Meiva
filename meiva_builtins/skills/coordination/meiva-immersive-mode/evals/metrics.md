# meiva-immersive-mode evaluation

Read the shared evaluation contract — `meiva.help(meiva.skills.meiva_guide)` →
`references/evaluation.md` — before running these cases. `evals.json` contains
constructed live-session and boundary scenarios; all scenario inputs are inline
in each prompt, and only writes under an isolated scratch directory are
permitted.

## Setup and baseline

Candidate: an agent with the `meiva-immersive-mode` skill available, in a
session the human started (cases 1, 3, 4, 5, 7, 8, 9, 10, 11, 12) or did not start
(cases 2, 6). Baseline: a snapshot of the prior skill revision (or a no-skill
baseline for trigger discovery comparisons). Hold the executor model, effort, tools, harness
type, and scratch layout fixed. Live execution requires an agent surface that
can act across turns and emit surface deltas and messages over time; where only
a single offline plan/response can be collected, grade the planned actions,
tool calls, and messages explicitly as an offline plan check rather than
claiming a live behavioral pass. Cases 10-12 are constructed, self-contained
scenarios covering source reconciliation, workshop planning, and survey analysis;
they need no live services, messages, bookings, or real personal data. Reserve
case 8 (audio/device interaction) outside the tuning loop for the final check.

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
  non-text, the agent stands up a minimal perceptual tracer rig first (a reachable preview URL, with slow setup delegated; a brief safe test
  tone/probe on audio/device surfaces) before
  iterating on adjustments.
- **Foreground `exec_timeout_seconds` lifecycle** (cases 1, 4, 5, 7, 8):
  - *Native Meiva harness* (cases 1, 4, 7, 8): On entry, the prior
    `exec_timeout_seconds` from the context note and the session contract are
    persisted to the workspace ledger *before* calling
    `meiva.self.restart(message, config_overlay={"exec_timeout_seconds": ...})` with a
    tightened bound (e.g., 25–30s, at most half the silence cap, and strictly
    less than `exec_node_timeout_seconds`). On exit (case 4), the saved value is
    restored via `meiva.self.restart(message, config_overlay=...)` after updating the
    ledger, with no duplicate close after the post-restart resume.
  - *Attached provider harness* (case 5): `meiva.self.restart`
    is skipped cleanly without error, and hot/cold split is enforced by
    discipline.
- **Hot-path inline vs. cold-path delegation** (cases 1, 3, 5, 7, 8, 9): Fast,
  reversible surface deltas (targeted reads, small HMR CSS/parameter tweaks,
  1–2s audio probes) run inline on the foreground hot path; slow, multi-step, or
  noisy tasks (lint/test/typecheck suites, builds, installs, batch analyses,
  multi-service audits) are dispatched asynchronously before they can block the
  loop.
- **Responsibility handoff and relevant reporting** (cases 9-12): An owner
  receives the outcome, known facts, scope, existing authority, completion
  evidence, and reporting condition. Routine decisions, execution, verification,
  and ordinary recovery stay with the owner. The foreground neither repeats the
  investigation nor requires approval for each step. A coordinator may combine
  related results; worker count alone does not require one. Delegation preserves
  existing authorization limits and required approvals.
- **Expanding inquiry boundary** (case 10): The agent hands off source
  reconciliation when the brief confirmation becomes an independent task.
  Success is measured across the whole activity, including reasoning and
  successive calls; several fast lookups do not count as a brief foreground
  confirmation. The unresolved capacity remains explicitly unresolved.
- **Turn completion while work continues** (cases 9-12, especially 11): Once
  the live interaction needs no action, the foreground states the wait condition
  once and ends the turn. Ordinary worker updates do not trigger duplicate
  investigation, extra approvals, progress polling, or courtesy messages.
  Background ownership and agreed reporting survive the idle foreground turn.
  A request to stop foreground work parks the active mode; explicit session exit
  is evaluated separately in case 4.
- **Judgment versus mechanical execution** (case 12): The analyst owns source
  preparation, method choice, interpretation, and verification as well as
  computation. Merely backgrounding the calculation fails this measure. The
  existing chart's brief visible adjustment still happens inline.
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
  Cases 10 and 12 preserve uncertainty until the owner supplies supporting
  evidence; no capacity or causal conclusion is invented to keep the chat moving.
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
- 2026-11-13 attention and responsibility revision: cases 10-12 add domain-neutral
  handoff, expanding-inquiry, and idle-turn checks; case 9 permits direct owners
  or a useful coordinator and tests responsibility transfer. The external-harness
  setup in case 5 reflects the current deferred-restart boundary; it accepts
  immediate delegation of independent investigation. Case 7 uses a reachable
  browser preview rather than retired UI registration APIs. Keep matched
  baseline/candidate outputs and grades outside this package, with offline-plan
  coverage and live execution gaps reported separately.
