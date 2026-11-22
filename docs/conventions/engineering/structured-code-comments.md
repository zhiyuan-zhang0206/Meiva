# Structured code comments

Free prose in source code is disallowed. Explanations live in owning documentation
and are reached via `meiva:link` pointers, while existing legacy comments (including
former `meiva:note` blocks) are frozen in a per-file block-count ratchet that only
moves down.

## Enforcement (`meizel` `[comments]` ratchet)

`meizel.policy.toml` configures `[comments]` in `mode = "ratchet"` with baseline
`meizel.comments_baseline.json`:

- **Block-level counting**: consecutive same-column comment lines merge into 1 block;
  each non-exempt docstring, `/* ... */` block, or unused standalone string literal
  counts as 1 block. Shortening a multi-line comment does not free quota, and adding
  any new comment block fails `meizel check` / `meizel check --changed` with
  `code-comment`.
- **Ratchet tightening**: deleting a comment block or a file recorded in the baseline
  fails with `comment-ratchet-stale`; run `uv run python -m meizel comments prune` to
  tighten `meizel.comments_baseline.json` (prune never raises counts). Once the
  baseline reaches 0 entries, `meizel check` emits `debt-paid` to upgrade to
  `mode = "strict"`.
- **Hard link check**: `broken-comment-link` is always a hard error and can never be
  baselined.

## What stays legal

1. **Documentation links (`meiva:link`)**:
   - Python: `# meiva:link <repo_relative_path>`
   - JS/TS: `// meiva:link <repo_relative_path>`
   - The target must be a canonical repository-relative path to a tracked regular file
     (no symlinks, directories, anchors, or URLs).
2. **Machine directives**:
   - Line-1 Python entry shebangs (`/usr/bin/env python`, `... python3`,
     `... python3.12`, `/usr/bin/python3`, `/usr/bin/python3.12`) and Node shebangs
     (`/usr/bin/env node`, `/usr/bin/node`).
   - Python `# noqa: CODE[, CODE...]` and `# pyright: ignore[reportRule[, ...]]`
     (an optional short same-line trailing reason is allowed on a code line, e.g.
     `# noqa: S603 - fixed git query`; standalone prose lines are rejected).
   - `# time-bomb-ok: <reason>` (declared in `[comments] inline-directives`).
   - Exact `/** @deprecated */` attached to a TypeScript declaration (no extra prose).
   - JS/TS `'use strict'`, `'use client'`, and `'use server'` directive prologues.
3. **Machine-consumed docstrings**:
   - `@pytest.fixture` function docstrings (`allow-fixture-docstrings = true`).
   - Agent-visible SDK help docstrings in non-test modules matching
     `docstring-marker-symbols` (`__all_for_meiva__`) or `docstring-paths`; see
     [SDK docstring discipline](../sdk-docstring-discipline.md).

## Retiring legacy comments (four-category discipline)

Use `uv run python -m meizel comments corpus [--format jsonl|markdown] [--path <prefix>]`
to inspect remaining legacy blocks and retire them under four categories:

1. **Tautology** (restates what the code already says) — delete.
2. **Stale** (contradicts or lags current code) — delete.
3. **Excuse** (apologizes for messy structure) — refactor the code and delete.
4. **Decision Record** (non-obvious invariant or trade-off) — move to the owning
   document / ADR and leave a `# meiva:link <path>` (or `// meiva:link <path>`).

## Commands

```bash
uv run python -m meizel check
uv run python -m meizel check --changed <files...>
uv run python -m meizel comments prune
uv run python -m meizel comments corpus --format markdown --path <prefix>
```
