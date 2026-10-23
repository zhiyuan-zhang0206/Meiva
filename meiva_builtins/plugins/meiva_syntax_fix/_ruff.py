# meiva:note
# Steps 2, 4 and 5 of the syntax-fix pipeline that run ruff: undefined-name detection, `ruff check
# --fix` and `ruff format`. Each takes the ruff to run as a value and builds its argv and reads its
# result through `_ruff_plan`.
from __future__ import annotations

from ._ruff_plan import (
    RuffRun,
    fix_argv,
    format_argv,
    rewritten_source,
    undefined_names,
    undefined_names_argv,
)
from ._ruff_run import run_ruff


# meiva:note
# Auto-fixes unused imports (F401), import ordering (I001) and other safe ruff rules. Diagnostics
# for unfixable issues go to stderr and are ignored; a ruff that crashes or prints nothing leaves
# the source unchanged.
def ruff_fix(code: str, ruff: RuffRun) -> str:
    done = run_ruff(fix_argv(ruff), code, ruff, step="check --fix")
    return code if done is None else rewritten_source(code, done.returncode, done.stdout)


# meiva:note
# Style-only normalization (quotes, spacing, line wrapping, trailing commas) on top of `ruff_fix`,
# gated by the syntax_fix_ruff_format flag. It restyles and never fixes an error, so a stage that
# gives up leaves the source as it was.
def ruff_format(code: str, ruff: RuffRun) -> str:
    done = run_ruff(format_argv(ruff), code, ruff, step="format")
    return code if done is None else rewritten_source(code, done.returncode, done.stdout)


# meiva:note
# The names ruff reports as undefined (F821). Empty when ruff is unavailable, crashes or the source
# does not parse (syntax errors surface downstream through compile()).
def ruff_undefined_names(code: str, ruff: RuffRun) -> frozenset[str]:
    done = run_ruff(undefined_names_argv(ruff), code, ruff, step="check --select F821")
    return frozenset() if done is None else undefined_names(done.returncode, done.stdout)
