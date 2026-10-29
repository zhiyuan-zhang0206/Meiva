---
type: doc
title: AGENTS.md size ceiling
description: The AGENTS.md line-count tiers and why its thresholds move only deliberately.
tags: [scripts, lint]
---

# AGENTS.md size ceiling

AGENTS.md is the entry point injected into every agent's context. The community
best practice (OpenAI / Anthropic Agent Skills, the AGENTS.md v1.1 proposal)
converges on a root file of about 100 lines — light enough to stay in context
permanently. Meiva's file was reduced from 385 to 81 lines (2026-07-14) by
extracting specialized rules into `docs/conventions/` under progressive
disclosure; `agents_md_size.py` keeps it from creeping back up.

AGENTS.md is measured in lines against one hard ceiling: below it the lint prints nothing and exits 0, past it the lint prints one error and exits 1. No band below the line is reported and no per-line or per-file exemption exists — a "room left" note would invite filling the last lines instead of moving a section out — and the constant moves only as a deliberate change in the same PR, justified in the commit message.

The hard ceiling fails the commit, so the entry point cannot grow back by
accident, and the message says where the overflow belongs: a section that only
matters while doing one task moves to `docs/conventions/`, which is read on
demand. The constant is the source of truth and this page does not restate its
value, so a deliberate bump cannot leave the prose lying.

Parent: [[scripts/lint/docs/lint.meiva.okf.md]].
