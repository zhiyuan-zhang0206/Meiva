---
name: models
description: "Selects registered LLMs and config overlays for Meiva workers. Use when choosing a worker model, reasoning effort, or speed tier."
---

# Model Selection — choosing a model, effort and speed tier

You pick a worker's model at spawn time:

```python
from uuid import uuid4

meiva.agents.spawn(prompt="...", config_overlay={"llm_model": model_id}, idempotency_key=str(uuid4()))
```

`model_id` is an id read from the live roster (next section), never a name
remembered from elsewhere. Omitting the overlay is a valid choice — the child
inherits the cluster default, which is the operator's standing choice. An
explicit overlay should mean a deliberate reason to differ from it.

This skill carries judgment that holds whichever models are registered: how to
choose, and how to spend. It names no model as preferred, required or
forbidden; that kind of policy lives in the places listed under "Where policy
lives".

## Where model names come from — enumerate, never hardcode

The roster moves. Never grep the source for a model id and never trust a name
copied from an old doc or an old spawn; list the current roster first:

- **Live list (agents, scripts)** — `GET /api/models` returns the spawnable
  roster grouped by provider, with current pricing, context window, effort
  options and `superseded_by` — the same data the spawn picker renders:

  ```python
  import httpx, os
  r = httpx.get(
      f"{os.environ['MEIVA_GATEWAY_URL']}/api/models",
      headers={"Authorization": f"Bearer {os.environ.get('MEIVA_API_TOKEN', '')}"},
      timeout=10,
  )
  models = r.json()["models"]  # id -> {provider, context_window, pricing,
                               #         reasoning_effort_options, ...}
  ```

- **Source of truth (repo-side)** — `base/lm/catalog/__init__.py`'s `ModelCatalog.models` and
  the derived `supported_models` (provider → spawnable ids). A model is
  selectable iff `spawnable=True`. Chat prices live in the provider plugin's
  `PriceRates` and are mirrored in `base/lm/pricing/pricing_catalog_archive.json` —
  the reviewed ledger the runtime reads (catalog-only services such as
  embeddings price from the archive alone). The frontend picker and
  `/api/models` both derive from this registry, so the registry is the only
  place a model is born.
- There is no dedicated `meiva models` CLI yet — the API above or the registry
  is how an agent lists models.

A config naming an id that is not on the roster fails spawn validation as an
unknown model; repair it by naming a current id from the roster.

## Where policy lives

Which models the operator wants used, and which are stopped, is configuration,
not skill text:

- The **cluster default** (`llm_model`) is what an omitted overlay inherits.
- A **per-agent config** (the spawn overlay, or `meiva.self.restart(config_overlay=...)`)
  pins one agent.
- The registry's roster flags express availability: a model with
  `spawnable=False` cannot be selected, and `superseded_by` hides a
  still-serviceable old model from the picker while older configs keep working.

A model that is stopped or withdrawn is expressed there — in the registry or
the config — never in this skill. When the user names a model to use or avoid,
that instruction is theirs to give per task or to record in the cluster
configuration; do not look for it here, and confirm the id is on the roster
before using it.

## Inference speed is part of the model ID

Select a Fast service through its independent roster ID (for example,
`gpt-6.1-sol-fast` or `claude-opus-5-5-fast`). It has its own price row and
usage identity; no additional Fast toggle is needed. Providers may serve the
same underlying model with a speed parameter. MiMo UltraSpeed already has a
native model ID. A spawn's `config_overlay` can pin any authorized ID; effort
still controls reasoning independently of inference speed.

Do not assume switching speed preserves the prompt cache. Claude explicitly
invalidates system and message caches when switching Fast/Standard; its tools
cache can remain valid. OpenAI documents cache routing and retention but does
not promise cross-speed reuse. Inspect reported cache-read tokens. Faster
inference can shorten a reasoning-heavy critical path; changing the ID does
not prescribe more progress reports or polling.

Fast services are priced for speed; weigh them against cost like any other
choice. Claude Fast requires account access to its research preview. Usage and
billing follow the actual service receipt, including OpenAI downgrades to
Standard.

References: [Claude caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching),
[Claude Fast](https://platform.claude.com/docs/en/build-with-claude/fast-mode),
[OpenAI Fast](https://developers.openai.com/api/docs/guides/fast-mode),
[OpenAI caching](https://developers.openai.com/api/docs/guides/prompt-caching)
(checked 2026-10-07).

## Reasoning effort

Effort sets reasoning depth independently of the model and of inference speed.
Each roster entry lists the levels it accepts (`reasoning_effort_options`); a
level outside that list is rejected, so read the entry rather than assume a
vocabulary. Higher effort costs more tokens and time; raise it for a task that
needs depth, not by default.

## Pareto dominance

Among the registered models, one dominates another when it is strictly smarter
at the same or lower cost, or equally smart at lower cost. Choosing a dominated
model is a mistake rather than a preference, so when two roster entries compare
that way, take the dominating one unless a stated constraint (a capability such
as vision, context window, speed, or the user's instruction) rules it out. A
`superseded_by` entry on the roster says the registry already made that
comparison. Compare on the live prices and capabilities, not on remembered ones.

## Spending: decomposition, verification, cost

Complexity is best absorbed by structure before it is absorbed by a more
expensive model. Three questions decide how much structure a sub-task gets:

1. **Open-ended judgment, or bounded procedure?** Split open-ended work into
   smaller, checkable steps first; a bounded procedure can run as one step.
2. **Blast radius of a wrong answer?** Large → add a verification wave (an
   independent cross-check, or peer review between agents) before trusting the
   result, rather than trusting one unverified output.
3. **Volume?** N parallel workers multiply cost by N — batch the work and
   converge. One-off calls barely matter.

## Looking at media: `meiva.self.attach` or `meiva.understand`

Both reach a model that can read the file; they differ in where the pixels go.

- `meiva.self.attach(path)` puts the file in your own context: you see it
  natively at your next step, and it stays in the conversation. It needs a
  model that can receive that file type; the roster entry lists the media types.
- `meiva.understand(...)` has a separate model answer in text, so your context
  stays small, and it can read types your own model cannot. Each content type
  (text, image, PDF, audio, video) has its own configured model:
  `MEIVA_UNDERSTAND_{TEXT,IMAGE,PDF,AUDIO,VIDEO}_MODEL`. Read the current values
  from the configuration, and the media types a model accepts from
  `GET /api/models`.

## Don't

- Don't hand an open-ended judgment task to any model and trust the output
  unverified — pair breadth with a cross-checking wave.
- Don't scatter hardcoded model names where the cluster default would do —
  an explicit overlay should mean a deliberate choice.
- Don't trust a model name from an old spawn, an old chat, or a stale doc —
  enumerate first (`GET /api/models` or the registry).
- Don't write model policy (preferred, required or stopped models) into a
  skill; it belongs to the cluster default, the per-agent config and the
  registry's roster flags.
