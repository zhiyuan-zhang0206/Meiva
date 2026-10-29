# meiva:note
# Report every call that cannot supply a parameter a signature just gained.
#
# Run: `.venv/bin/python scripts/audit/call_sites.py --spec '[MODULE:]FUNCTION:PARAMETER[:POSITION]'`
# `[--root DIR] [--json] [--enforce]`; repeat `--spec` to check several parameters.
#
# A required parameter added to a shared callable is a compile error at every caller that does not
# pass it — but only where the type check looks. A caller under `scripts/tests`, `tests/e2e` or a
# plugin's own tree can sit outside a scoped run, and a call that hands the value over positionally
# never appears in a search for `parameter=`. The judge is `analysis/calls.py`; this walks the tree
# the operator names, prints, and owns the exit code.
#
# The spec grammar, the four verdicts and what a `checked` of zero means are
# [[scripts/audit/docs/call-sites.meiva.okf.md]].
#
# The default reports and exits 0: a caller may be right to leave the parameter to a wrapper, and
# that judgment belongs to the author who made the change. `--enforce` exits 1 when any call is
# `missing`, which is the form a CI job can use; an `unproven` call never fails it, because one
# `*args` helper up the stack would otherwise decide the verdict for every caller behind it.
# meiva:link scripts/audit/docs/call-sites.meiva.okf.md

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from pathlib import Path

from scripts.audit.analysis.calls import (
    MISSING,
    call_site_payload,
    judge_calls,
    parse_call_spec,
    render_call_sites,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# meiva:note
# The trees a walk must never enter: an installed venv holds thousands of files, and a worktree
# holds another checkout of this one, whose call sites are the other branch's business.
_SKIPPED = frozenset(
    {
        ".git",
        ".venv",
        ".worktrees",
        ".mypy_cache",
        ".pytest_cache",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
    }
)


# meiva:note
# Every Python source below `root` as (tree-relative path, text). A file that cannot be read as text
# is skipped here rather than in the judge: the caller is what touches the disk.
def sources(root: Path) -> Iterator[tuple[str, str]]:
    for path in sorted(root.rglob("*.py")):
        if any(part in _SKIPPED for part in path.relative_to(root).parts):
            continue
        try:
            yield path.relative_to(root).as_posix(), path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report call sites that cannot supply a parameter."
    )
    parser.add_argument(
        "--spec",
        action="append",
        default=[],
        metavar="[MODULE:]FUNCTION:PARAMETER[:POSITION]",
        help="a required parameter to look for; repeatable",
    )
    parser.add_argument("--root", default=str(_REPO_ROOT), help="the tree to walk")
    parser.add_argument("--json", action="store_true", help="print the findings as JSON")
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="exit 1 when a call cannot supply the parameter (the default reports only)",
    )
    arguments = parser.parse_args(argv)
    try:
        specs = [parse_call_spec(text) for text in arguments.spec]
    except ValueError as error:
        parser.error(str(error))
        raise
    if not specs:
        parser.error("at least one --spec is required")
    findings, judged = judge_calls(sources(Path(arguments.root)), specs)
    if arguments.json:
        print(json.dumps(call_site_payload(findings, judged), indent=2))
    else:
        print(render_call_sites(findings, judged))
    if arguments.enforce and any(finding.verdict == MISSING for finding in findings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
