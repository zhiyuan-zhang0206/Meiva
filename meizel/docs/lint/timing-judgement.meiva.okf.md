---
type: doc
title: The timing-judgement rule (no wall clock as a test's judge)
description: What `scripts/lint/timing_judgement.py` reports, why a wall-clock bound cannot prove what it names, who decides when it fires, and where the counted-fact alternatives are written.
tags: [lint, evaluation, tests]
---

# The timing-judgement rule

Entry: `meizel/owners/timing_judgement.py` (module docstring carries the rule) behind the
`meizel/owners/timing_judgement_cli.py` shell; runs as the `lint-timing-judgement` pre-commit
hook and over every pytest host in CI.

## What it reports (the fact)

A comparison inside `assert` that puts a wall-clock quantity against a number. The
quantity is a clock read (`time.monotonic()`, `perf_counter()`, `process_time()`,
`time.time()`, a loop's `.time()`, or one imported by name — `from time import monotonic`),
a name bound to one, or a difference of two of them. The bound is a numeric literal **or a
module-level name bound to one**, resolved through `_B = _A * 2`.
Both directions are reported — an upper bound flakes under load, a lower bound fails a
correct implementation that got faster. The message names the enclosing function and
quotes the comparison.

## Why it exists

`#7725` made the rule necessary: a probe re-consumed its budget per segment, the defect
was installed on purpose, and `assert elapsed < 0.5` **stayed green**. A bound loose
enough not to flake on a loaded runner is loose enough to pass the bug it was written to
catch — the assertion proves "nothing hung", never "the budget held". The same shape then
reached CI as a flake in `test_a_blackhole_stays_inside_the_single_total_budget`
(`#5812`), and the four wall-clock repairs of 2026-10-30 (`#7739`, `#7756`, `#7770`,
`#7789`) each replaced one with a counted fact: the envelopes the probe sent, the trigger
the commit set, the answer the listener returned, the requests parked on the semaphore.

The rule does not repair the existing sites — `#5822` does that one at a time. It stops
the next one, which is the only thing a rule can do about a class of bug this easy to
rewrite by hand.

## Who owns the decision when it fires

Whoever is changing that test, at that moment, and the marker is where the decision is
recorded. Two answers are legitimate, and they are different claims:

- **It names a budget.** Then the assertion must read the counted fact the budget governs
  (envelopes, rounds, records, the value the code returned). Deleting the assertion is not
  the fix; the budget is still untested.
- **It only catches a hang.** Then `# timing-guard: only catches a hang` on the assert or
  the line above, and the bound must be far outside any real duration. A hang guard at
  1.5× the budget is a flake with a comment.
- **The clock is injected.** Then `# timing-guard: injected clock` — the quantity comes
  from a clock the test controls (`base/clock` timers), and the comparison is exact.

"It was flaky" is not one of the three: it is the symptom this rule exists to remove.

## Where to look

- `#5821` inventory (`~/.meiva/workspaces/7138/audits/wall-clock-assertions.md` at the
  time of writing): 66 sites, and the three shapes a repair takes — inject a controllable
  clock, assert the number of envelopes, assert the number of rounds.
- `docs/conventions/engineering/flaky-tests.md` — the leak/flake families this one sits
  beside.
- `meizel/owners/timing_judgement_baseline.json` — the sites that predate the rule, keyed
  by path, function and comparison with literals masked, so retuning a constant keeps its
  entry while adding a comparison does not. Regenerate with `--write-baseline`; the diff
  is the review, and a regenerated file that grows without a matching repair is the
  signal that someone silenced the rule instead of answering it.

  **The ledger only goes down.** When a gap in the rule is found, the answer is: widen the
  rule, add the sites it now sees to the baseline, and say in the change why they were
  invisible before — never edit the number by hand. Two such widenings on 2026-10-30
  (`from time import monotonic`, then module-level numeric bounds) found `4` and `17` sites
  that had been counted as governed. The uncomfortable part is that the same wave that
  moved bounds into named constants (`hang_guard_s`, `_*_BOUND_S`) moved them into the
  blind spot: a governance change can create invisible debt, which is why the ledger is
  stated as "sites the rule can see", never as "sites that exist".

## What the bound resolution does not do

Written down because "the rule misses my site" is the report this will get, and the answer
has to be the same both times:

- **No cross-module names.** `assert elapsed < tests.support.BUDGET` is not resolved; the
  name has to be bound in the module being scanned.
- **No calls.** `assert elapsed < budget()` stays invisible. A bound computed at run time
  is a decision about what the test controls, not something a scanner can read.
- **No names bound inside a function.** `budget = 5` in the test body is not a bound.
- **No clock against clock.** `deadline = monotonic() + 30; ... assert monotonic() < deadline`
  is a timeout with a named deadline, not the claim `elapsed < 0.5`, and the rule leaves it
  alone.
- **No class attributes as bounds.** `class C: B = 5` with `assert clock - started < C.B` is not
  resolved; only a plain module-level name is.
- **A function that binds no clock is not scanned at all.** The scan skips a function whose body
  binds no clock name, so a bare `assert time.monotonic() < 30` there is invisible. Measured
  2026-10-30: removing the filter adds 10 sites and every one of them is a false positive —
  assertions on an injected clock's own reading (`assert timers.monotonic() == 10.0`). The filter
  stays; the price is that an instant guard with no binding is invisible. Closing it means
  treating `time.<clock>()` as an instant read only when nothing is bound and
  `timers.monotonic()` as object state — one more decision, not a deleted line.

Each of these is a widening of what the rule claims, so each needs its own decision and its
own review — not an implementation detail slipped in beside a fix.

## What it does not judge

`asyncio.wait_for(..., timeout=X)` — hundreds of call sites, and the audited sample had
the correct shape (wait for the real event, with a loose backstop). `--hints` prints the
count so the scope stays visible without a per-line report.
