---
type: doc
title: Which models a harness runs
description: The registry's per-model harness declaration behind the spawn form's model column, and what it does and does not claim.
tags: [base, agents, lm, frontend]
---

# Which models a harness runs

Split out of [[base/agents/docs/harness/harness.meiva.okf.md|Agent harness
identity]], which owns the value set, its immutability and the fork rule. This
node owns the matrix the spawn form's model column is built from.

The model registry owns it: `ModelSpec.external_harnesses` (`base/lm/registry.py`)
names the external harnesses that can run a model, and `GET /api/models`
publishes the answer per model as `harnesses`, built-in runtime first.

`meiva` runs every spawnable model — it speaks every provider through the
configured API keys — so `harnesses_for` implies it instead of asking each spec to
list it. An empty set is the ordinary case for a vendor with no external CLI;
today `claude-*` declares `claude_code`, `gpt-*` `codex` and `gemini-*`
`antigravity`, and `with_fast_variants` carries a Standard model's declaration onto
the Fast service it derives.

`validate_spec` refuses a value outside the `Harness` enum instead of dropping
it, so a typo in a provider plugin fails the catalog build rather than silently
emptying a CLI's model column.

The declaration answers one question: may this model be chosen for an agent born
on that harness? It is not the launch probe — whether a machine can start a
harness at all stays `harness_availability()` and its `launchable` verdict — and
it is not what the CLI is told to run: Claude Code starts with its adapter's
model default and Codex inherits its CLI configuration today, while the agent row's model remains the one the graph
budgets and compacts against. The spawn form labels these external-harness
choices as Meiva history model and summary effort, and explains that the external CLI uses its own adapter or CLI
model settings. The form keeps a model in its column only
while the chosen harness appears in the published list, and holds no
harness-to-provider table of its own, so assigning a model to another harness is
one line in its `ModelSpec` and reaches that column with no frontend change.
