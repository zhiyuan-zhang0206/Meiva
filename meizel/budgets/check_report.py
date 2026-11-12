# meiva:note
# Check's report composition: source measurements use the same scope and cap owners as
# structure lint. Independent owners can contribute explicit data snapshots; the checker
# never runs inspected code or discovers a user's live Memory pool.
from __future__ import annotations

import hashlib
import json
import sys
import tomllib
from collections.abc import Callable, Collection, Mapping, Sequence

from meizel.budgets import budget_plans
from meizel.budgets.report import BudgetRow, source_rows
from meizel.budgets.snapshot import SOURCE_KINDS, Snapshot, decode_snapshot, render_snapshots
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
# An unborn repository still has real index/worktree measurements. Missing history makes
# provenance partial; it does not erase known readings or invent a revision.
def _revision(repo: Checkout) -> str:
    try:
        return repo.run_text("rev-parse", "--verify", "HEAD").strip()
    except MeizelError:
        return "not-provided"


# meiva:note
# This measurement identity binds changed claims to these exact readings, including dirty
# worktree contents. HEAD identifies source history; it is not mislabeled as a clean snapshot.
def _source_snapshot(
    repo: Checkout,
    read_roles: Callable[[Checkout], Mapping[str, tuple[str, ...]]],
    changed: Collection[str],
) -> Snapshot:
    manifest = tomllib.loads(repo.load_text("meizel.toml", default=""))
    roles: Mapping[str, tuple[str, ...]] = read_roles(repo) if "roles" in manifest else {}
    rows = _source_rows(repo, roles, changed)
    identity = hashlib.sha256(
        json.dumps(
            [
                (row.kind, row.location, row.current, row.limit, row.changed)
                for row in sorted(rows, key=lambda row: (row.kind, row.location))
            ],
        ).encode()
    ).hexdigest()
    provenance = {
        "producer": "meizel/source-budgets/v1",
        "source_revision": _revision(repo),
        "root": str(repo.root),
        "snapshot": identity,
        "changed_root": str(repo.root),
        "changed_snapshot": identity,
        "inventory": "tracked regular Python/worktree text; tracked Git direct entries; measurement hash",
        "units": "python-lines=splitlines; directory-entries=tracked direct entries",
        "effective_limits": f"budget_plans: HARD_CEILING={budget_plans.HARD_CEILING}; "
        f"DIRECTORY_CEILING={budget_plans.DIRECTORY_CEILING}",
    }
    return Snapshot("source", tuple(rows), SOURCE_KINDS, provenance)


# meiva:note
# A failed snapshot never becomes a fabricated zero row. File failures retain their explicit
# requested label even when unreadable bytes cannot identify an external owner.
def _collect(
    repo: Checkout,
    read_roles: Callable[[Checkout], Mapping[str, tuple[str, ...]]],
    changed: Collection[str],
    inputs: Sequence[str],
) -> list[Snapshot]:
    snapshots: list[Snapshot] = []
    try:
        snapshots.append(_source_snapshot(repo, read_roles, changed))
    except (OSError, UnicodeError, ValueError, TypeError, MeizelError) as error:
        snapshots.append(Snapshot("source", (), SOURCE_KINDS, unavailable={"source": str(error)}))
    for name in inputs:
        try:
            snapshots.append(decode_snapshot(repo.load_text(name)))
        except (OSError, UnicodeError, ValueError, TypeError, MeizelError) as error:
            snapshots.append(Snapshot(f"input:{name}", (), unavailable={name: str(error)}))
    return snapshots


def _write(repo: Checkout, table: str) -> None:
    path = repo.root / ".cache/meizel/budgets.md"
    try:
        repo.apply_plan(EditPlan(writes={path: table}, dirs=(path.parent,)))
        print("meizel: budget headroom: .cache/meizel/budgets.md (full table, report only)")
    except OSError as error:
        print(f"meizel: budget artifact unavailable: {error}; full table follows", file=sys.stderr)
        print(table)


# meiva:note
# Coverage/provenance stays with its rows in the full table and changed summary. Missing pool
# inputs are not-provided; collection/parse failures unavailable; missing kinds/provenance partial.
# Every state is advisory and keeps the dependency check's original exit status.
def emit(
    repo: Checkout,
    read_roles: Callable[[Checkout], Mapping[str, tuple[str, ...]]],
    changed: Collection[str] | None,
    inputs: Sequence[str],
) -> None:
    snapshots = _collect(repo, read_roles, changed if changed is not None else (), inputs)
    _write(repo, render_snapshots(snapshots))
    print(
        "meizel: budget coverage: "
        + "; ".join(f"{snapshot.owner}={snapshot.coverage.value}" for snapshot in snapshots)
    )
    if not any(snapshot.owner == "memory-pool" for snapshot in snapshots):
        print("meizel: memory-pool=not-provided (live-owner budgets not measured)")
    for snapshot in snapshots:
        for kind, reason in snapshot.unavailable.items():
            print(
                f"meizel: budget report: {kind}: measurements unavailable: {reason}",
                file=sys.stderr,
            )
    if any(row.attention for snapshot in snapshots for row in snapshot.rows):
        print("meizel: changed budgets with <=10% remaining (report only):", file=sys.stderr)
        print(render_snapshots(snapshots, attention_only=True), file=sys.stderr, end="")
