---
name: meiva-immersive-mode
description: "Runs a tight-loop live session with the human present across a shared perceptual surface (chat, live preview page, or device/audio output) while heavy work runs in the background. Use when the human explicitly starts a live session (immersive on, live pairing) — offer once when live steering helps, never enter on your own."
---

# Immersive Mode — Tight-Loop Shared-Surface Control

A live-session discipline for a foreground agent whose human is present and
actively steering. Keep the human's **primary perceptual surface** responsive in
a tight feedback loop while slow, multi-step, or noisy engineering work runs
asynchronously in the background.

## Core model: shared perceptual surface

When the human is live, their attention anchors on one **primary surface**:

- **Conversation stream**: direct rapid Q&A, alignment, or live triage in chat.
- **Live visual or app surface**: a hot-reloading dev server registered with
  `meiva.ui.show(name, port, ...)` or a static/prototype directory served via
  `meiva.ui.serve(dir, name, port, ...)`.
- **Physical or multimodal output**: audio test tones on speakers, GUI actions,
  or hardware probes the human perceives directly.

Two distinct clocks govern the loop:

1. **Surface cadence (seconds)**: On a visual, app, or audio/device surface,
   each foreground tweak should produce an **observable delta** (updated frame,
   played tone, visible state change) within seconds. While that surface carries
   the feedback, chat becomes a lightweight **cue & control** channel — one or
   two sentences naming what changed, what subjective effect to look or listen
   for, or what choice is needed. Never write long text explanations that pull
   the human's eyes or ears off the primary surface.
2. **Silence watchdog cap (default 60s, human-adjustable)**: The upper bound on
   unexplained quiet when the primary surface has no observable delta (such as
   waiting on background work or running a chat-only pass). Either land a real
   delta/finding inside the cap or send a concise pre-notice naming what is
   running and when the next signal arrives. It is a stall guard, not a 60-second
   chatter timer; filler is banned.

## When it applies

- **Explicitly human-triggered**: The human starts the mode ("immersive on",
  "let's do this live", or accepting an offer). Never enter or extend a session
  on your own — where the human places live attention is exclusively their call.
- **At most one offer**: Offer a live pass once when real-time steering would
  materially help ("want to tune this live together?"), then drop it.
- **Session-scoped**: Applies only to this foreground session and its active
  tasks. Outside it, `reduce-context-switch-for-human` governs unchanged.

## Entry sequence & foreground timeout tightening

1. **Persist the workspace ledger first**: Create or update a durable ledger file
   in your workspace recording:
   - the prior `exec_timeout_seconds` read from your context note (default `300`s
     on the native harness),
   - the session contract: primary surface, silence watchdog cap (default `60`s),
     hot/cold split, and exit reversion,
   - live rig details (port, page name, probe command), delegated owners, wait
     states, human decisions with one-line rationales, and evidence paths.
2. **Announce the contract**: State the primary surface, silence watchdog cap,
   foreground/background split, tightened foreground exec ceiling (when native),
   and that the human can end the mode at any time.
3. **Tighten `exec_timeout_seconds` (native Meiva harness)**: Call
   `meiva.self.restart(config_overlay={"exec_timeout_seconds": <tight_s>})` to set
   a hard single-call ceiling (typically `25`–`30`s, at most half the silence
   watchdog cap, and strictly below `exec_node_timeout_seconds`). Kernel timeout
   enforcement prevents an accidental slow foreground call from wedging the live
   loop. Because `restart` replaces the process and resumes the turn, writing the
   ledger *before* `restart` ensures the resumed turn sees entry is complete and
   proceeds directly without repeating the announcement.
   - **External CLI / attached harnesses**: In attached provider CLI or
     impersonation sessions where `meiva.self.restart` is unavailable or refused,
     skip `restart` on both entry and exit, record the harness boundary in the
     ledger, and enforce the same hot/cold split by discipline alone.
4. **Establish the live feedback rig first**: When the primary surface is
   non-text, your first operational move is a minimal tracer bullet that stands
   up the live target the human perceives — start the HMR/dev server and register
   it with `meiva.ui.show`, or fire a 1-second audio/device probe to verify the
   output path. If initial build or setup is slow, immediately dispatch the heavy
   setup to the background while aligning details in chat or serving a lightweight
   static prototype via `meiva.ui.serve`, switching over once the live rig boots.

## Foreground inline (hot path) vs. background delegate (cold path)

- **Foreground inline (hot path)** — strictly operations that finish well inside
  the tightened `exec_timeout_seconds` window *and* directly drive a reversible
  observable delta on the primary surface or answer the human immediately:
  - Direct conversation, brief cues, and decision prompts;
  - Single-step targeted file reads or narrow state checks;
  - Small hot-reload edits or parameter tweaks on an already-running live rig
    (e.g., CSS/layout adjustments over HMR, filter/config knob changes);
  - Triggering a short audio, GUI, or hardware probe.
- **Background delegate (cold path)** — everything with uncertain duration,
  multiple steps, heavy compute, or noisy output:
  - Package installation, builds, and compilation;
  - Multi-file or cross-module code changes and refactors;
  - Test suites, typecheckers, and linters;
  - Broad codebase searches and deep research.
  Dispatch cold-path work via `meiva.agents.spawn`, `meiva.shell.run_background`,
  or `meiva.watcher` and rely on event wakeups rather than foreground polling.
  Pair both paths freely: apply a small UI tweak inline so the human sees the
  HMR delta in seconds while running typecheck and regression tests in the
  background to verify safety.

## Operating rules

1. **Human preempts**: A new human message preempts — finish at most the current
   fast inline step, answer or steer immediately, then continue.
2. **Sub-orchestrate fan-outs (>2 concurrent background tasks)**: Every
   `send_message` to the foreground agent wakes it for a turn. When background
   work needs more than two parallel tasks, do **not** attach leaf workers
   directly to the foreground; spawn **one background sub-orchestrator** (or a
   `meiva-dynamic-workflow` coordinator) to manage all leaf workers. Leaf workers
   report only to that sub-orchestrator; only aggregated milestones,
   human-authority blockers, or verified deliverables reach the foreground.
3. **Protect the loop**: Batch non-urgent fleet arrivals into natural session
   pauses; surface immediately only what needs human judgment now.
4. **Signal, never filler**: Every chat turn carries an observable-delta cue, a
   finding, a decision, or an explicit wait notice. Never manufacture chatter,
   rush verification, or spawn cosmetic workers to look busy.

## Exiting

When the human ends the session:

1. Update the workspace ledger with final state and open background owners.
2. Send a concise close: what changed, what remains delegated (with owners and
   ledger path), and the next milestone under default batched reporting.
3. On the native Meiva harness, call
   `meiva.self.restart(config_overlay={"exec_timeout_seconds": <saved_value>})`
   to restore the original timeout recorded in the ledger; when the post-restart
   turn resumes, see the closed ledger and stop quietly without repeating the
   closing message.
4. Revert fully to `reduce-context-switch-for-human` (queue results, batch
   summaries, no live push). If the human falls silent mid-session without
   closing, park state in the ledger, state what you are waiting on once, and
   wait quietly.

## Relationship to other skills

`reduce-context-switch-for-human` governs when the human is away; this skill
governs the complementary regime while the human is live on a shared surface.
When the live goal is building the human's own causal model so they can predict
and judge the subject themselves, load `meiva-workflow`'s
`align/references/understanding-loop.md` via
`meiva.help(meiva.skills.meiva_workflow)` for evidence shape while keeping
surface pacing and timeout control here.
