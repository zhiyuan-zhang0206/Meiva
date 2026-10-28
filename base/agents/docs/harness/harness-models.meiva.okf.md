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

The model registry owns it: `ModelSpec.external_harnesses` maps each external
harness to a `HarnessModelSpec`. A binding declares its exact effort ladder,
concrete default, and optional effort-to-CLI-model mapping. `GET /api/models`
publishes the supported harnesses and their `harness_settings`; the frontend
owns no provider table or effort conversion.

`meiva` runs every spawnable model through its configured API keys.
`harnesses_for` therefore implies the native runtime. External bindings name
only executable selections: Claude Code, Codex and Kimi use the registered
model ID; Antigravity's Gemini 4 Argon, Gemini 3.8 Flash, Claude Opus 5.5 and
Claude Sonnet 5.5 bindings translate low/medium/high into the exact observed
`agy models` IDs ending in that effort. Antigravity offers only these declared
CLI variants. API Fast service variants remain
native-only until an adapter explicitly supports that service selection.

`validate_spec` rejects unknown harnesses; bindings reject invalid defaults
and incomplete CLI model mappings. External effort ladders are independent
of native API tuning: Codex model/list advertises low through ultra on the
models that support it, and omits API-only none. Spawn
preflight and the birth transaction reject a model/effort combination the
chosen harness cannot execute; they never substitute a CLI default for an
unsupported selection.

The stored `llm_model` and `reasoning_effort` select the execution session.
The runner resolves that binding and passes it through `bridge_for` and
`open_session`. Claude receives SDK model/effort options; Codex receives them
on every `turn/start`, including after thread resume; Kimi receives ACP config
options; Antigravity receives the resolved full model ID on its command line.
External agents use the CLI's authentication and do not construct a native
chat model or require its provider API key.

Model and Effort mean execution settings in both spawn layouts. External
harnesses own their context, compaction and continuation. Meiva retains their
basic timeline projection, without a separate history/summary model or native
compaction controls. Whether a machine can start the chosen harness remains
the launch probe's independent `launchable` verdict.


New external births stamp `harness_model_selection: 1` in `birth_config`. Only
that version opts `llm_model` and `reasoning_effort` into CLI execution. Older
external sessions used those pins for native history settings, so they retain
the adapter/CLI's original defaults when relaunched or resumed. Their stored
history choices are not reinterpreted as execution settings. Unknown selection
versions fail explicitly; no existing row is rewritten during deployment.
