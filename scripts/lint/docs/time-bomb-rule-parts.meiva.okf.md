---
type: doc
title: The time-bomb rule's parts
description: What each module of the time-bomb rule owns, and the vocabulary facts the code no longer restates comment by comment.
tags: [lint, evaluation]
---

# The time-bomb rule's parts

The rule is one judgment about two kinds of test-time bomb, split so each part has
one job. `scripts/lint/diagnostics/time_bomb.py` stays the documented entry (its
module docstring carries the three rules and why they exist) and re-exports `main`
from `entry.py`.

- `sources.py` — the shared vocabulary: which parameter names are a caller-visible
  clock, which calls read the real clock, which names are window-shaped, and what
  counts as a fixed calendar literal. It reads nothing.
- `index.py` — the module view the rules ask: lazy ASTs, top-level functions,
  fixed-instant names, and the per-function summary rule 2 quotes.
- `judgment.py` — the three rules and their messages.
- `entry.py` — scan directories, target resolution, exit code.

## The vocabulary facts

- **Clock parameters** are `now`, `now_utc`, `at`, `as_of`, `clock`, `when`,
  `timestamp`, `instant`. `deadline` is deliberately absent: it is a monotonic
  budget, not a wall-clock instant.
- **Real clocks** are `datetime.now` / `datetime.utcnow` / `datetime.today` and
  `time.time`. `monotonic` is deliberately absent: a duration measured against it
  is independent of the wall-clock relation that makes a fixed-instant window
  boundary a time bomb.
- **Window-shaped names** (`day`, `date`, `since`, `until`, `window_start`,
  `window_end`) are the names whose fixed calendar value rots the day the window
  rolls past it.
- **Opt-out** is the inline `# time-bomb-ok: <reason>` marker on the judged line.

## Reading a module

The index parses a module only when a rule reaches it — through a call, an import,
or the judged file itself — and remembers the answer for the run. A module is
in scope when its first dotted segment is one of the scanned directories or
`tests`, and it is never read through a `mirrors/` path or a `__pycache__`.

A fixed-instant name is a module-level name bound to `datetime(...)`/`date(...)`,
directly or through other such names; the fixed-instant family of a module is the
transitive closure of those assignments.

## The messages are part of the contract

Every finding is `file:line: <reason>`, and the wording is quoted by tests and by
reviewers walking a failure back to the rule. The split moved the code, not the
sentences: `tests/lint/diagnostics/tests/runtime_rules/test_lint_time_bomb.py`
pins them, and a change of wording there is a change of contract.

Parent: [[scripts/lint/docs/lint.meiva.okf.md]].
