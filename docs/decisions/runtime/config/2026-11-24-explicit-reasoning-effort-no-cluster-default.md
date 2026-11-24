# Reasoning effort is chosen with the model, never defaulted cluster-wide

## Context

`MEIVA_REASONING_EFFORT` was a `cluster-default`, `per_agent` field. It could pin one effort for
every agent that did not name its own, yet an effort is only meaningful for one model: a level a
model offers is a level another one rejects (`deepseek-flash` takes `high`/`max`, a Claude
extended-thinking model `none`/`high`, an external CLI model whatever its probe reports, some none
at all). A pin that is right for the model it was written for silently breaks or changes the
behaviour of every other one, and the field's remaining readers were accidental: the conversation
labeler daemon read it for its own, unrelated model call, and an agent whose stored config lacked
an effort fell through to the live cluster value. In practice the value was empty on the production
cluster, and births already resolved a concrete effort (the model's registry default, the
external harness probe's default) behind the caller's back.

`llm_model` has no such default: a root spawn and a cross-harness spawn must name it, a child
inherits its parent's, a fork inherits its source's.

## Decision

The effort follows the same rule as the model.

- Every entry that requires `llm_model` — a spawn from outside an agent (Gateway `POST /api/keyed/v1/agents`,
  SDK `meiva.agents.spawn`, the draft routes), a spawn onto another harness than the parent's, a
  restart that switches harness — also requires `reasoning_effort`.
- A child that names a model other than its parent's must name that model's effort too: an effort
  never carries over to a different model, the same holds for a same-harness restart that changes
  `llm_model`. A child that names neither inherits its parent's pair; a fork inherits its source's
  `birth_config` untouched.
- A model with effort levels needs one of them (`InvalidModelConfig` / HTTP 4xx otherwise). A model
  without levels — an external CLI model whose probe reports `reasoning_effort_options == ()` —
  takes none; naming one stays an error. No placeholder value is invented for it. Every native
  spawnable model has levels (the registry refuses one that does not).
- The `reasoning_effort` field stays in Settings only as the per-agent pin the overlay and the
  agent slices are keyed on (an overlay key must be a framework Settings field, as for `llm_model`),
  now `scope: "agent"`, `writable: False`, `default None`: it is no longer cluster configuration,
  appears nowhere in Settings, and the labeler no longer reads it (it resolves its model's default).
- Stored configs are never re-judged: launch, resurrect, transfer and fork read what the row holds,
  so an agent born before this rule keeps running.

## Alternatives rejected

- **Delete the Settings field outright.** The overlay key whitelist, the pin map and the
  `ModelOverrides` slice are all derived from the registry's per-agent fields; removing it would
  need a second pin registry just for this one value. The field keeps a single meaning instead.
- **Keep defaulting to the model's registry default.** It is the behaviour this replaces: it hid a
  choice that changes cost and latency by a large factor behind an omitted key.
- **Always require a value, with a placeholder for effort-less models.** Invents a fake value that
  every reader would have to know to ignore.
- **Let a same-harness child that changes model fall back to the model default.** That is the same
  implicit default under another name, and the one case an existing caller would trip on silently.

## Consequences

- A caller that spawned with only `{"llm_model": ...}` from outside an agent, or with a model other
  than its parent's, now fails fast with the supported levels in the message; the repository's
  callers, skills and docs name the effort alongside the model.
- The Settings page has no Reasoning effort row; the General page's Models card goes with it.
- A leftover `MEIVA_REASONING_EFFORT` line in a unit `.env` is not read by any config surface; it
  is only the pin's alias and has no effect on agents that carry their own effort, which is every
  agent born from now on.

<!-- Supersedes the cluster-wide pin in decisions/frontend/ui/2026-08-01-spawn-picker-concrete-effort-defaults.md -->
