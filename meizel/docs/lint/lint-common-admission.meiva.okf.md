---
type: doc
title: "Lint admission — read the checkout once per run"
description: "Why a lint run admits the repository's files once, what keeps the reuse from outliving the run, and how to see the counters when a hook is slow."
tags: [tool, evaluation]
---

# Read the checkout once per run

Every lint that judges a directory asks `meizel.lint_common` for the checked-out
files. Git owns that admission: tracked regular files plus new non-ignored files,
with any path reached through a symlinked parent rejected (`meizel/checkout.py`).

The ask used to be per target, and each ask re-read `git ls-files` and then
re-checked every component of every path for symlinks. Measured on 2026-10-30 for
three changed files:

| rule | before | after |
|---|---|---|
| `lint-time-bomb` | 28.5 s | 1.2 s |
| `lint-clock-lattice` | 14.1 s | 1.3 s |

`time_bomb` asked 21 times in one run: 75 s of a 92 s profiled run went into
`checkout.tracked_files`, and the remaining cost was the per-target filter walking
every admitted name as a `Path` (234k constructions, `is_relative_to` per name).

## The scope, and why it cannot outlive the run

`lint_common.admission()` brackets one run: inside it the checkout is read
once and later asks reuse that file set; the exit clears it. A caller outside the
scope always goes back to git, so a file created between two runs is judged by the
second run. The reuse is deliberately not process-global and not on disk — the
repository's own file set is the only key that would be honest, and a per-run scope
makes the question moot.

A lint whose inputs include files outside the changed set (the clock lattice's
family modules) widens `changed_scope` instead of caching by changed path: editing
a family module re-judges every file, which is a correctness rule, not a speed
one.

## Counters

Every run of a lint that admits files ends with one line on stderr:

```
lint admission: 9123 files, 1 read, 20 reused
```

`reads` above 1 means a code path opened a second scope; `reused` is how much the
run saved. `lint_common.admission.stats()` returns the same numbers to a test.
A green pre-commit hook hides its output, so the line appears in a failing hook's
log and in a direct run.

## What pins it

`meizel/tests/test_lint_common.py` covers the scope (read once, ends with the run,
an unrelated touch does not change the admitted set);
`meizel/owners/tests/test_lint_clock_lattice.py` covers it end to end — a
violation created between two runs fails the second one, and a touched unrelated
file keeps the verdict. Restoring a cross-run cache turns that end-to-end case red
(`assert 0 == 1`), which is the point of keeping it.

Parent: [[meizel/docs/meizel.meiva.okf.md]].
