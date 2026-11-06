---
type: doc
title: reduce-context-switch-for-human — Interrupt Discipline
description: Durable publications and questions preserve context while milestone reporting protects attention.
tags:
- fleet
- agent-instruction
- user-communication
---

# reduce-context-switch-for-human — Interrupt Discipline

## What it is

Every push costs a human a context switch. This skill (`plugins/meiva_fleet/skills/reduce-context-switch-for-human/SKILL.md`) stipulates: when the user's attention is elsewhere, default to **silence** — work lands in a queue that the user retrieves at their own pace.

## Discipline

Ask collects user decisions; publish or the configured text protocol delivers
meaningful milestones and results. Unread publications and unanswered questions
remain durable. New entries never overwrite them. The bundled skill owns the
reporting cadence and delegation routing. Explicit answers settle questions;
reading or ignoring an entry never grants authorization.

## Push Posture: Only Push for Truly Urgent Matters

Out-of-band pushes are only allowed for truly urgent matters: irreversible risk en route / entirely blocked on user / user explicitly asked to be woken. Everything else stays in the queue. Since 2026-09-20 the core rules are a **platform default**: the system prompt carries them to every agent (section gated by the Fleet image field `reduce_context_switch`, default on) — the switch gates the guidance; push timing itself stays the agent's judgment, not framework enforcement. push = deliver to where the human actually is, channel chosen by agent — the skill determines *when*, not *how*.

## Key Dependencies

- [[meiva_builtins/plugins/meiva_fleet/docs/notify.meiva.okf.md|Notify]] — queue channel itself (`meiva.ui.ask`)
- [[meiva_builtins/plugins/meiva_fleet/skills/meiva-fleet/docs/meiva-fleet.meiva.okf.md|meiva-fleet skill]] — fleet general outline of the same communication dichotomy
- [[meiva_builtins/skills/docs/skills.meiva.okf.md|Skills index]] — available communication skills
