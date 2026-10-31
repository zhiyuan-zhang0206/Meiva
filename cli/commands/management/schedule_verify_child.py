# meiva:note
# The per-script check child of `meiva schedules verify` — stdin: one schedule script.
#
# Run as `python -m cli.commands.management.schedule_verify_child` from the repo root, in the
# checkout's own venv (the interpreter that would launch the schedule). Stdlib only at module level.
# Three checks; nothing from the script's body runs:
#
# 1. **Imports** — `compile()` the script, then execute ONLY its top-level import statements. A moved
#    module (the #2678 / R3 Wave-2 drift class) fails here.
# 2. **Names** — every `Name` the script reads (any scope) must be one it binds, a builtin, or an
#    implicit module global (`__name__`, ...). This is the read the import check cannot see: after the
#    `shared` -> `base` rename, `_ROOT = Path(base.__file__)` reads a `base` nothing imports — the
#    import check stays green and the first real fire dies with `NameError` (2026-10-01 audit: three
#    of 16 copies). Conservative by construction: a name bound anywhere counts as bound (a PEP 695
#    type parameter's own name included: `def f[T]`, `class C[T]`, `type A[T]`), annotation
#    expressions are never read (house style: `from __future__ import annotations`; type-parameter
#    bounds and defaults likewise), and a `from x import *` disables the check outright. Names only
#    a runtime would create (`exec`/`eval`, `globals()` writes) are not modeled: a read of one is
#    reported, never guessed around.
# 3. **Call sites** — every call whose callee resolves through those imports to repo code
#    (`catch_up(...)`, `schedules.jobs.catchup.fire_slot_once(...)`, `Database.from_settings()`) must
#    `inspect.signature(...).bind` the arguments the script passes. A signature that moved on
#    (2026-10-03: `catch_up()` and `fire_slot_once()` gained a required `db`) fails here, where the
#    import check stays green. Only the argument SHAPE is bound (count and keyword names; values are
#    placeholders), and a call that cannot be bound statically is skipped, never guessed: `*args` /
#    `**kwargs` at the call site, a callee reached through an instance or a local variable, a name the
#    script rebinds, a callable with no introspectable signature. The agent SDK (`meiva.*`) is skipped
#    too, names and signatures alike: plugins install its namespaces (`meiva.tasks`) and wrap its
#    functions at load time, so the static module is neither the set of names nor the call contract.
#
# Output: the last stdout line is the verdict — `CHILD-OK`, `CHILD-COMPILE-ERROR:<line>:<msg>` (rc 3),
# `CHILD-MODULE:<name>` (rc 4), `CHILD-EXC:<type>:<msg>` (rc 5), `CHILD-UNDEF:<detail> | <detail>` (rc 6),
# or `CHILD-SIG:<detail> | <detail>` (rc 7).
#

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path
from typing import Any

from cli.commands.management import schedule_verify_checks

_DETAIL_MAX = 160

# What a script may read without binding it: the builtins and the module-internal implicit
# globals the runtime provides.


def _is_repo_code(obj: object, root: Path) -> bool:
    try:
        source = inspect.getsourcefile(inspect.unwrap(obj))  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return schedule_verify_checks.is_repo_source(None, root=root)
    return schedule_verify_checks.is_repo_source(
        None if source is None else Path(source).resolve(), root=root
    )


def main() -> int:
    src = sys.stdin.read()
    try:
        compile(src, "<schedule>", "exec")
        tree = ast.parse(src)
    except SyntaxError as exc:
        print(f"CHILD-COMPILE-ERROR:{exc.lineno}:{exc.msg}")
        return 3
    segments = [
        segment
        for node in tree.body
        if isinstance(node, ast.Import | ast.ImportFrom)
        and (segment := ast.get_source_segment(src, node))
    ]
    namespace: dict[str, Any] = {"__name__": "dryimport"}
    try:
        exec(compile("\n".join(segments), "<imports-only>", "exec"), namespace)
    except ModuleNotFoundError as exc:
        print(f"CHILD-MODULE:{exc.name}")
        return 4
    except Exception as exc:
        print(f"CHILD-EXC:{type(exc).__name__}:{str(exc)[:120].replace(chr(10), ' ')}")
        return 5
    namespace.pop("__builtins__", None)
    undefined = schedule_verify_checks.undefined_name_problems(tree)
    if undefined:
        print("CHILD-UNDEF:" + " | ".join(name[:_DETAIL_MAX] for name in undefined))
        return 6
    root = Path.cwd().resolve()
    problems = schedule_verify_checks.call_site_problems(
        tree, namespace, is_repo=lambda target: _is_repo_code(target, root)
    )
    if problems:
        print("CHILD-SIG:" + " | ".join(p.replace("\n", " ")[:_DETAIL_MAX] for p in problems))
        return 7
    print("CHILD-OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
