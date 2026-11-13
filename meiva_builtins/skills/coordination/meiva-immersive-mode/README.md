# meiva-immersive-mode skill — tight-loop live sessions with the human present

## What it is

`meiva-immersive-mode` organizes a live session around attention, shared
interaction, and task responsibility. The foreground agent helps the human
understand, see or hear feedback, and make choices; background owners complete
independent work. Research, documents, planning, data analysis, creative work,
device operation, and software work use the same criteria.

Pacing follows a **shared perceptual surface**: conversation, a shared document
or chart, an app preview shared through a reachable URL, or physical output
such as audio tones and device actions. [meiva.ui](../../../../meiva/docs/ui.md)
provides user delivery and questions.

On non-text surfaces, the agent stands up a minimal **live feedback rig first**
(delegating slow setup while aligning in chat). Surface-level **observable deltas**
close in seconds with chat reduced to brief **cue & control** notes, while a **silence
watchdog cap** (default 60s) guards against unannounced stalls when no surface
delta is occurring.

The [skill's foreground contract](SKILL.md#foreground-responsibility) judges
the whole activity, including reasoning between calls. A brief confirmation
that expands into an independent inquiry goes to an owner with responsibility
for decisions, execution, verification, and ordinary recovery within the existing
authority. The foreground avoids duplicating that investigation or approving
every step. Mechanical work can run in a background process; sustained judgment
needs an agent owner. After the handoff, the foreground continues the live
interaction or ends the turn waiting for the human or an agreed result.

On the native Meiva harness, entry records the prior `exec_timeout_seconds` in a
durable workspace ledger and tightens the single-call ceiling via
[meiva.self.restart](../../../../meiva/self/README.md) (`config_overlay`),
restoring the saved value on exit. This single-call bound complements the
responsibility contract; it does not bound a sequence of short calls or the
reasoning between them.

## Relationship to the default reporting discipline

The default ([reduce-context-switch-for-human](../../../plugins/meiva_fleet/skills/reduce-context-switch-for-human/README.md))
protects the human's attention while they are away: results queue, updates
batch, and nothing depends on live presence. `meiva-immersive-mode` is the
complementary regime while the human chooses to be present on a shared surface.
Both regimes let background owners continue independently; they differ in surface
cadence, foreground execution bounds, and when signals reach the human.

## Boundaries

- **Human-triggered entry only**: The agent may offer a live pass at most once
  when real-time steering helps, and never starts or extends a session on its
  own (2026-10-17 user ruling).
- **Session-scoped and reversible**: Scoped to the active foreground session;
  explicit exit hands off via the workspace ledger, restores
  `exec_timeout_seconds` on the native harness, and resumes default batch
  reporting.
- **Harness boundary**: An attached provider harness (Claude Code, Codex,
  Antigravity) skips `config_overlay` timeout changes (the exec ceiling does not
  govern its loop, and a restart there waits for the turn to end) and relies on
  hot/cold execution discipline.
- **Worker updates**: Owners report agreed results and relevant blockers. The
  foreground batches non-urgent arrivals and may use a coordinator for related
  work; worker count alone does not mandate another layer.

## Validation

Cases live in `evals/evals.json` and measures in `evals/metrics.md`, covering
entry/exit timeout lifecycle, non-text perceptual surfaces (HMR UI preview and
audio/device probes), human preemption, responsibility handoff, expanding
inquiries, waiting without repeated intervention, and positive and negative
trigger boundaries. Offline plans and live execution are reported separately.

[Skills index](../../README.md)
