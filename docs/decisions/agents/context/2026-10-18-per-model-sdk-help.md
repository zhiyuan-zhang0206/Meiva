# SDK help is per model: stated visibility and stated text

## Context

Models differ in training data and behaviour, so they should not read one identical SDK manual.
The prompt already varies per model: `ModelTuning` carries the section switches
(`prompt_user_tone_enabled` and thirteen peers) and `agent_communication_style`, declared on
each `ModelSpec` in the provider plugins — Claude models, for example, ship with the user-tone
section off (user ruling 2026-09-03). The SDK surface varied only by capability: a text-only
model's help hides `meiva.self.attach`.

Two kinds of variation were missing, and the user required both before any wording work:
a model that must not see certain SDK members, and a model that reads *different text* for the
same member. `sdk_disable` cannot serve either: it removes a member process-wide from an agent
setting, not from a model's documentation.

## Decision

`ModelSpec` gains two fields, declared next to the model's other facts:

- `sdk_hidden_members: frozenset[str]` — dotted `meiva` paths this model's help omits. The
  render merges it with the capability gate; the member still exists and still calls.
- `sdk_doc_style: str | None` — the documentation variant this model reads.

A target declares its variants where its contract lives:

```python
@doc_variants(terse="Say hi.", detailed="Greet the person by name, warmly.")
def greet(name: str) -> str:
    """Greet someone by name."""
```

Both render paths build one `ModelHelpProfile` from the catalog — the system prompt's SDK
reference and an exec turn's `meiva.help()` — so they cannot disagree. A style the target does
not declare falls back to the docstring.

## Alternatives rejected

- **Per-model text tables in the catalog** (`ModelSpec.sdk_docs = {"meiva.files.read": "..."}`):
  a function's contract would live in a provider plugin file, far from the function, and every
  model that wants a variant adds another copy in another file. The variant stays a second
  owner of the same contract.
- **Template docstrings** (`"""...{limit}..."""` formatted at render): no types, no
  validation, and the failure surfaces at render time instead of authoring time.
- **Rewriting `__doc__` at runtime**: a global side effect ordered by import, invisible to
  static checks.
- **Reusing `sdk_disable` for model differences**: it is an agent setting that removes the
  member from the process, which is a different question from what a model should read.

## Consequences

- Variation is declared data: a toggle set, a style name, an added member set. The same fact
  still has one owner, and every variant is a complete contract rather than a patch over one.
- A malformed declaration fails catalog validation: a hidden entry must be a dotted path under
  `meiva`, a style must be a plain identifier. A typo cannot silently hide nothing.
- Every variant is agent-facing text: `scripts/lint/agent_docstrings.py` now scans
  `doc_variants` texts for CJK, implementation leaks and Markdown, exactly like docstrings.
- A model may name a style nothing implements; the render falls back silently. A test asserts
  every style a catalog model names is declared by some target, so a dead declaration fails CI
  once one exists.
- Variants are extra text to maintain. They are justified only when two models must read
  different things — a restated docstring with different words is a review defect, not a use.
- Hiding reaches the SDK reference — `meiva.help(meiva.X)` and the prompt's expanded sections —
  not the one-line namespace index in the base prompt, which stays model-independent by design
  (`agent/graph/prompt/_base_prompt.py`). Hiding a whole namespace from that index would need a
  separate decision about per-model prompt text.
