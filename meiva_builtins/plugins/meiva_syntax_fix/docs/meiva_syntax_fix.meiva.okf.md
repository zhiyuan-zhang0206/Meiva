---
type: doc
title: meiva_syntax_fix — Syntax Fix Plugin
description: '`meiva_syntax_fix` automatically fixes common Python syntax errors before code execution. It is a defensive fix (rather than letting the agent fix itself), because the round-trip cost is high—having the model regenerate vs silently fixing and then executing, the latter is faster and usually correct.'
tags:
- extensions
- plugin
- agent-extension
---

# meiva_syntax_fix — Syntax Fix Plugin

## What it is

`meiva_syntax_fix` automatically fixes common Python syntax errors before code execution. It is a defensive fix (rather than letting the agent fix itself), because the round-trip cost is high—having the model regenerate vs silently fixing and then executing, the latter is faster and usually correct.

## Registered hooks

### Pre-execution fix (before_exec hook)

```python
class _SyntaxFixHook(Hook):
    async def __call__(
        self, state: AgentState, _runtime: Runtime[MeivaContext], config: RunnableConfig, /
    ) -> dict | None: ...

syntax_fix_before_exec = _SyntaxFixHook()
# declared in contribute(): before_exec=(syntax_fix_before_exec,)
```

Each `execute_code` call in the last `AIMessage.tool_calls` is repaired independently. IDs and sibling calls are preserved, and only the matching content block is updated.
Multi-stage pipeline: the main pipeline in `plugin.py`, followed by a batch of deterministic fix batteries
(`_deterministic_fixes.py`), and finally compile validation + LLM fallback.

### 1. Chinese/fullwidth punctuation → ASCII (manual, not covered by ruff)

- `_translate_outside_strings` based on Python `tokenize` only replaces **outside strings**, preserving Chinese content inside strings
- fullwidth comma → `,`, fullwidth period → `.`, fullwidth colon → `:`, fullwidth quotation marks → ASCII quotation marks
  (`_FULLWIDTH_QUOTE_MAP` first, `_PUNCT_MAP` after)
- Must run first, otherwise subsequent ruff/tokenize parsing fails

### 2. Missing imports → auto-add (ruff F821 + mapping table)

A missing optional Ruff executable warns on each skipped stage and leaves
the source unchanged; there is no process-wide warning cache.

- ruff `F821` (undefined name, scope-aware) detects undefined names
- Manual mapping tables like `_BARE_NAME_IMPORTS`: `Path`, `datetime`, `json`, `np`→`numpy`,
  `pd`→`pandas`, **`meiva`** (added to auto-import list), etc.
- Only fixes names known in the mapping table; unknown undefined names are left alone (for agent to handle)

### 3. Illegal escape sequence repair (manual, tokenize)

- Invalid escapes emit `SyntaxWarning` in Python 3.12; `_fix_invalid_escapes` does tokenize-level repair, e.g., `\s` → `\\s` (inside string literals)

### 4. `ruff check --fix`

- Handles unused imports, import sorting, trailing whitespace, blank lines, and other safe automatic fixes

### 5. `ruff format` (gated)

- Controlled by the agent's `sandbox.syntax_fix_ruff_format` slice; beyond the deterministically correct path, only does style normalization

### 6. Deterministic fix batteries (`_deterministic_fixes.py` / `apply_all_deterministic_fixes`)

After ruff, a batch of fixers targets failure patterns observed in production logs (unicode punctuation, string line breaks,
unclosed triple quotes, indentation, trailing backslash, missing comma, bracket pairing, empty f-string, unterminated string to triple quotes,
nested triple quotes, inner quote escaping). **Each fix is independently compile-guarded**: only adopted if it makes the source **compile**,
first compile-passing fix wins—prevents "legitimate code corrupted" or "greedy delimiter search silently merging source".

### 7. compile validation + LLM fallback

`compile()` pre-check; if still failing, calls `llm_repair_syntax` to let the model fix it (only adopted if its result compiles),
if that still fails, a single-call turn returns a compile-error ToolMessage directly. In a multi-call turn, the ordinary child reports that call's syntax error while sibling calls still execute.

## Key dependencies

- [[agent/hooks/docs/hooks.meiva.okf.md]] — before_exec hook
- [[agent/graph/exec/docs/exec.meiva.okf.md]] — code execution sandbox (where before_exec hook runs)
- `_deterministic_fixes.py` (26631 bytes) — compile-guarded deterministic fix batteries (step 6), fixer table strongly typed as `list[tuple[str, Callable[[str], tuple[str, int]]]]`

## Configuration

- ruff config in `pyproject.toml [tool.ruff]`
- `sandbox.syntax_fix_ruff_format` (agent slice): whether to run step 5 `ruff format`
- `lm.llm_override` (cluster setting, read from the agent's slices by the hook): when set, the LLM repair model is the one this `module:factory` builds
- Chinese punctuation maps: `_PUNCT_MAP` / `_FULLWIDTH_QUOTE_MAP`
- Import maps: `_BARE_NAME_IMPORTS` etc. (stdlib + third-party aliases + `meiva`)

## Where the decisions live

The hook is the composition root: it reads the agent's slices once per turn into `FixInputs`
(the `ruff_format` flag, the `RuffRun` and the `RepairInputs`) and hands them down as values.
`_ruff_plan.py` is the strict pure half of the ruff stages: the argv each stage runs, how an exit
status and stdout become the stage's result (`rewritten_source`, `undefined_names`) and the warning
a stage that gave up logs (`give_up_warning`). `_ruff_run.py` is the shell that runs the argv and
resolves the executable beside the interpreter, never through the ambient PATH. `_repair_text.py`
is the strict text half of the LLM repair (prompts, the rendered SyntaxError, the reply cleanup);
`_llm_repair.py` is its loop. The tests give a stage a `RuffRun` over a script in `tmp_path` (a
real child that echoes, sleeps past a short budget or exits non-zero) and give the repair step a
model through the `llm_override` the agent's slices carry.

## Notes

- This is the only plugin that modifies agent code — other plugins only change messages or prompts
- Stage order is load-bearing: punctuation fixed first (otherwise ruff/tokenize parsing fails), then imports, escapes, ruff --fix,
  ruff format, deterministic batteries, finally compile + LLM fallback
- "Silent fix" strategy: the agent usually does not know its code was fixed (if it compiles, it executes directly)
- compile-guard is a key lesson: verify that a fixer not only leaves good input unchanged but also fixes bad input with correct semantics (AST not collapsed / arguments not lost)

## Repair test premises

`tests/syntax_fix/test_fix_invalid_escapes.py` keeps source snippets as literal
inputs, including malformed Python. The format-disabled case uses a duplicate
import to guarantee a lint fix, then checks that formatting stays disabled;
it must not depend on whether the current Ruff configuration repairs a missing
final newline. Deterministic compile-error cases point `llm_override` at a repair model that is not configured,
so their fallback assertions do not call a provider.
Spacing assertions pin the format setting. Triple-quote closer candidates are
compile-checked so an earlier embedded delimiter cannot end the repaired string.
