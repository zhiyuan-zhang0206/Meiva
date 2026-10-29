"""Forbid the wall-clock time-bomb: exact-equality test assertions on
values derived from a fixed instant while the derivation can reach the real
clock, and fixed calendar fixtures bound to window-shaped names in tests.

Run: `.venv/bin/python scripts/lint/diagnostics/time_bomb.py [path ...]` (defaults to the
source dirs + tests/; an explicit path that does not exist is an error (stderr +
exit 1) rather than a silent no-op). Also run automatically via pre-commit.

## Why

A fixed-instant constant (`CUTOVER_AT = datetime(2026, 8, 23, 11, 0,
tzinfo=UTC)`) that production folds against the *real* clock makes every
window-boundary result a function of the wall clock. A test that asserts that
result with exact equality (`assert lifecycle_call["from_"] ==
CUTOVER_AT`) is correct only while `now` keeps a particular
relation to the constant — and that relation expires the moment the wall clock
passes the constant's cutoff (2026-08-30: two fixed-instant tests went
deterministically red within seven days of each other, each red run ejecting
the whole merge-queue batch). The fix pattern is to *pin* the clock (pass
`now=`/`at=`), assert with a tolerance (`pytest.approx`), or assert the
terminal monotone behavior — never an exact value whose correctness depends on
an unstated wall-clock relation.

## The rules

Three checks, all AST-based (no imports of app code; runs anywhere the source
tree is present — the same zero-dependency shape as the other `scripts/`
lints):

1. **Clock-threading into the fixed-instant world (source).** An in-repo
   function that *accepts* a clock parameter (`now`, `now_utc`, `at`,
   `as_of`, `clock`, `when`, `timestamp`, `instant`) must not call a
   real-now-using function whose clock cannot be reached while the clock
   parameter is live: every call in its body to a callee that
   (transitively) reaches a fixed-instant constant module and uses the real
   clock must either thread the callee's clock parameter or sit inside the
   caller's `param is None -> real now` fallback. Calling a window helper
   that folds a fixed instant against the real clock from inside
   `compute_rollup(now_utc=...)` without threading the clock is exactly the
   2026-08-30 rollup bomb's seedling: the parameter is a promise the code
   does not keep, so the test that "pins" `now_utc` is still asserting
   against the real clock. (The original seam — the label-window split
   accepting `now=` — has since been removed; the rule stays general.)

2. **Exact equality on a fixed instant with an unpinned real-now
   derivation (test).** In a test function, an exact `==`/`!=` comparison
   whose compared expression references a repo fixed-instant constant (or a
   local derived from one) is a time bomb when the same function's value
   derivation can reach the real clock — via `datetime.now`/`time.time`, an
   in-repo call whose clock is not pinned (a retention floor without
   `now=`), or an opaque HTTP call (`client.get(...)`) whose internals the
   linter cannot audit. Pinning is recognized when the callee's clock
   parameter is passed a fixed-instant-derived expression *and* the
   callee's own clock parameter actually guards its real-now paths
   (summary computed by rule 1). A tolerance (`pytest.approx`, or
   `abs(...) < n`) is always allowed. Deliberate exceptions carry an
   inline `# time-bomb-ok: <reason>` comment on the asserted line.

3. **Fixed calendar fixtures bound to window-shaped names (test).** A fixed
   calendar literal (`"2026-09-06"`, `date(2026, 6, 9)`,
   `datetime(2026, 7, 22, 18, tzinfo=UTC)`) bound to a window-shaped name
   (`day`, `date`, `since`, `until`, `window_start`, `window_end`) as a dict
   value, keyword argument, or plain assignment is a time bomb the moment the
   window it feeds is evaluated against the real clock: the fixture rots the
   day the window rolls past it. Derive the value from the clock (or from the
   request under test), or carry `# time-bomb-ok: <reason>` on the binding.
   Scanned positions are the dict value, keyword argument, and plain
   assignment — attribute/subscript targets, `AnnAssign`, and values laundered
   through other names are deliberately out of scope.

Scope: rule 1 scans non-test source dirs only; rules 2 and 3 scan test files
only (the top-level `tests/` and every package's own `tests/` directory). Error
format `file:line: <reason>` + non-zero exit.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.lint.diagnostics.time_bomb_rule import main  # noqa: E402 - standalone script

__all__ = ["main"]

if __name__ == "__main__":
    raise SystemExit(main())
