# meiva:note
# Check's report composition: source measurements use the same scope and cap owners as
# structure lint. Independent owners can contribute explicit data snapshots; the checker
# never runs inspected code or discovers a user's live Memory pool.
from __future__ import annotations

import sys
import tomllib
from collections.abc import Callable, Collection, Mapping, Sequence

from meizel.budgets import budget_plans
from meizel.budgets.report import BudgetRow, decode_rows, render_table, source_rows
from meizel.graph.model import MeizelError
from meizel.graph.universe import Checkout, EditPlan
from meizel.roles import select_packages

__all__ = ["emit"]


def _source_rows(
    repo: Checkout, roles: Mapping[str, tuple[str, ...]], changed: Collection[str]
) -> list[BudgetRow]:
    scopes = select_packages(roles, "application", "standalone", "tests", "tooling")
    texts = repo.universe().python_files
    judged_texts = {
        repo.root / name: repo.load_text(name)
        for name in texts
        if any(
            budget_plans.judged_path(repo.root / name, repo.root / scope, repo.root) is not None
            for scope in scopes
        )
    }
    children = budget_plans.tracked_children(repo.run_text("ls-files", "-z"))
    return source_rows(
        judged_texts,
        repo.root,
        budget_plans.judged_directories(children, [repo.root], repo.root),
        changed,
        budget_plans.HARD_CEILING,
        budget_plans.DIRECTORY_CEILING,
    )


# meiva:note
# Supplemental availability and a writable artifact are reporting concerns. Their failure is
# visible, but preserves the dependency check's exit status. The complete table is never limited
# by --limit; only changed, near-cap rows enter the human summary.
def emit(
    repo: Checkout,
    read_roles: Callable[[Checkout], Mapping[str, tuple[str, ...]]],
    changed: Collection[str] | None,
    inputs: Sequence[str],
) -> None:
    if changed is None:
        changed = ()
    rows: list[BudgetRow] = []
    unavailable: list[str] = []
    try:
        manifest = tomllib.loads(repo.load_text("meizel.toml", default=""))
        roles: Mapping[str, tuple[str, ...]] = read_roles(repo) if "roles" in manifest else {}
        rows.extend(_source_rows(repo, roles, changed))
    except (OSError, UnicodeError, ValueError, TypeError, MeizelError) as error:
        unavailable.append(f"source measurements unavailable: {error}")
    for name in inputs:
        try:
            rows.extend(decode_rows(repo.load_text(name)))
        except (OSError, UnicodeError, ValueError, TypeError, MeizelError) as error:
            unavailable.append(f"{name}: supplemental measurements unavailable: {error}")
    table = render_table(rows)
    if unavailable:
        table += "\nUnavailable measurements:\n" + "\n".join(unavailable) + "\n"
    path = repo.root / ".cache/meizel/budgets.md"
    try:
        repo.apply_plan(EditPlan(writes={path: table}, dirs=(path.parent,)))
        print("meizel: budget headroom: .cache/meizel/budgets.md (full table, report only)")
    except OSError as error:
        print(f"meizel: budget artifact unavailable: {error}; full table follows", file=sys.stderr)
        print(table)
    for message in unavailable:
        print(f"meizel: budget report: {message}", file=sys.stderr)
    attention = [row for row in rows if row.attention]
    if attention:
        print("meizel: changed budgets with <=10% remaining (report only):", file=sys.stderr)
        print(render_table(attention), file=sys.stderr, end="")
