---
name: meiva-immersive-mode
description: "Keeps live interaction responsive across chat, shared content, or device/audio output while background owners complete independent work. Use when the human explicitly starts a live session (immersive on, live pairing) — offer once when live steering helps, never enter on your own."
---

# Immersive Mode — Tight-Loop Shared-Surface Control

A live-session discipline organized around the human's attention, shared
interaction, and task responsibility. The foreground agent understands intent,
answers questions, presents feedback, and helps the human make choices.
Background owners carry work that needs independent exploration or sustained
execution. This applies to research, documents, planning, data analysis,
creative work, device operation, and software work alike.

## Foreground responsibility

- **Serve the current interaction.** Keep brief, predictable actions inline
  when they directly answer the human or produce feedback on the shared
  surface. Judge the whole activity, including reasoning between calls, rather
  than the speed of each individual tool call.
- **Hand off an expanding inquiry.** When a brief confirmation opens up an
  independent task, delegate it with the question, known facts, and required
  evidence. Avoid a chain of "one more quick check" while the human waits.
- **Transfer responsibility with the work.** Name an owner and the outcome,
  scope, existing authority, completion evidence, and reporting condition.
  Within that scope the owner makes routine decisions, executes, verifies,
  and resolves ordinary problems. Reuse a suitable owner or start a worker;
  keep the live discussion moving instead of researching the same question
  yourself or approving each intermediate step. Delegation grants no new
  authority and preserves required approvals.
- **Return attention to the human.** After the handoff, continue the current
  discussion or perceptible adjustment. When neither needs action, state the
  wait condition once and end the turn. Waiting for the human or an agreed
  result is a valid live-session state; ongoing background work does not keep
  the foreground turn open. A request to stop foreground work parks this
  session after any necessary handoff; keep immersive mode available for the
  next interaction unless the human also ends the live session.

## Core model: shared perceptual surface

When the human is live, their attention anchors on one **primary surface**:

- **Conversation stream**: direct rapid Q&A, alignment, or live triage in chat.
- **Live visual or app surface**: a shared document, chart, presentation, app
  preview, or context panel (`to_user.md` / `meiva.ui.publish()`).
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
   - live surface details (document, URL, probe command), delegated owners, wait
     states, human decisions with one-line rationales, and evidence paths.
2. **Announce the contract**: State the primary surface, silence watchdog cap,
   foreground/background split, tightened foreground exec ceiling (when native),
   and that the human can end the mode at any time.
3. **Tighten `exec_timeout_seconds` (native Meiva harness)**: Call
   `meiva.self.restart("Enter immersive mode", config_overlay={"exec_timeout_seconds": <tight_s>})` to set
   a hard single-call ceiling (typically `25`–`30`s, at most half the silence
   watchdog cap, and strictly below `exec_node_timeout_seconds`). Kernel timeout
   enforcement prevents an accidental slow foreground call from wedging the live
   loop. Because `restart` replaces the process and resumes the turn, writing the
   ledger *before* `restart` ensures the resumed turn sees entry is complete and
   proceeds directly without repeating the announcement.
   - **Attached harnesses**: In an attached provider harness (Claude Code,
     Codex, Antigravity) the exec ceiling does not govern the loop, and
     `meiva.self.restart` there applies only after the current turn ends. Skip
     `restart` on both entry and exit, record the harness boundary in the
     ledger, and enforce the same hot/cold split by discipline alone.
4. **Establish the live feedback rig first**: When the primary surface is
   non-text, your first operational move is a minimal tracer bullet that stands
   up the live target the human perceives — open the shared document or preview,
   update `<workspace>/to_user.md`, or fire a 1-second audio/device probe. Delegate
   slow setup while aligning details in chat, switching over once it is ready.
   Hand off existing independent work on entry too; preparing a background job
   must not become another foreground project.

## Foreground inline (hot path) vs. background delegate (cold path)

Choose by attention cost, predictable duration, need for independent exploration,
and immediate feedback. A short command can be part of a long investigation.

- **Foreground inline (hot path)**: direct conversation, brief cues, a bounded
  factual confirmation, or a small reversible adjustment to the active surface
  (wording, chart view, layout, audio level, device setting). The whole step
  should answer the human or produce perceptible feedback within seconds and
  stay well inside the native execution ceiling where it applies.
- **Background delegate (cold path)**: source comparison, document synthesis,
  open-ended investigation, data processing, preparation, or verification that
  requires sustained attention, uncertain duration, or multiple dependent steps.
  Software builds and test suites are examples of the same rule.

Use an agent owner for work needing judgment, follow-up, or interpretation.
`meiva.agents.spawn` can assign a new worker; an existing suitable owner can
continue. A self-contained mechanical operation can use
`meiva.shell.run_background`, with `meiva.watcher` for temporary event waits.
Putting a command in the background only delegates its execution: if preparing,
interpreting, or repairing it needs sustained attention, give that work an owner
too. Rely on completion events rather than foreground polling. Pair brief live
adjustments with independent background verification when useful.

## Operating rules

1. **Human preempts**: A new human message preempts — finish at most the current
   fast inline step, answer or steer immediately, then continue.
2. **Protect the loop**: User delivery (`user_facing` / explicit delivery) is
   separate from internal worker wakeups. Agree on a useful result or milestone;
   let owners handle routine intermediate updates. Batch non-urgent arrivals
   into natural pauses. Routine progress needs no acknowledgment or renewed
   instructions; record useful state and end the internal turn when no action
   is needed. A coordinator can aggregate related work when needed;
   worker count alone does not require another management layer. Surface results
   relevant to the current discussion and decisions requiring the human. If
   evidence reveals a material error or risk, route the specific concern to its
   owner or the human as appropriate; avoid reopening the entire task yourself.
3. **Signal, never filler**: Every chat turn carries an observable-delta cue, a
   finding, a decision, or an explicit wait notice. Never manufacture chatter,
   rush verification, or spawn cosmetic workers to look busy.

## Exiting

When the human ends the session:

1. Update the workspace ledger with final state and open background owners.
2. Send a concise close: what changed, what remains delegated (with owners and
   ledger path), and the next milestone under default batched reporting.
3. On the native Meiva harness, call
   `meiva.self.restart("Exit immersive mode", config_overlay={"exec_timeout_seconds": <saved_value>})`
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
