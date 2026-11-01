"""`debt record` and `debt prune`: the only commands that change a baseline.

`record` is the only way a baseline grows. `check` and `bootstrap` never write one.
"""

# meiva:note
# This module also reads one: `read_baselines` is the read half of `write_baseline`, kept beside it so
# the one module that knows how a baseline sits in a manifest owns both directions.

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

from meizel.contracts.analysis import Analysis, analyze
from meizel.contracts.debt import (
    CYCLE_EDGE,
    TABLE_KEY,
    DebtEntry,
    DebtTable,
    entry_value,
    local_edge,
    parse_debt,
    set_debt,
    total,
)
from meizel.contracts.ledger import resolve_debt_conflicts, tally
from meizel.contracts.manifest import Manifest, manifest_path
from meizel.graph.graphs import feedback_edges, unneeded_back_edges
from meizel.graph.model import Code, Diagnostic, MeizelError
from meizel.graph.universe import Checkout, EditPlan, Universe

__all__ = [
    "Baselines",
    "created_notice",
    "prune",
    "read_baselines",
    "record",
    "resolve",
    "write_baseline",
]


Baselines = dict[str, DebtTable]


def _parse(text: str, where: str) -> DebtTable:
    problems: list[str] = []
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise MeizelError(f"{where}: invalid TOML: {error}") from error
    table = parse_debt(document[TABLE_KEY], problems.append) if TABLE_KEY in document else {}
    if problems:
        raise MeizelError(f"{where}: " + "; ".join(problems))
    return table


# meiva:note
# The recorded debt of every directory of the working tree that has any.
def read_baselines(universe: Universe, texts: Mapping[str, str] | None = None) -> Baselines:
    found: Baselines = {}
    for directory in sorted(universe.manifest_dirs):
        path = manifest_path(directory)
        raw = texts[path] if texts is not None and path in texts else universe.repo.load_text(path)
        table = _parse(raw, path)
        if table:
            found[directory] = table
    return found


def _within(path: str, targets: list[str]) -> bool:
    return any(not t or path == t or path.startswith(f"{t}/") for t in targets)


def _refuse_malformed(analysis: Analysis, action: str) -> None:
    malformed = [d for d in analysis.diagnostics if d.code is Code.MANIFEST_DEBT]
    if malformed:
        shown = "\n".join(d.render() for d in malformed[:5])
        raise MeizelError(f"cannot {action} while a baseline is malformed:\n{shown}")


def _copy(table: DebtTable) -> DebtTable:
    return {code: dict(rows) for code, rows in table.items()}


# meiva:note
# A recorded entry raised to at least `count`, keeping who owns it and what it waits for: a change in
# the count is not a change of owner.
def _raised(entry: DebtEntry | None, count: int) -> DebtEntry:
    if entry is None:
        return DebtEntry(count)
    return DebtEntry(max(entry.count, count), entry.owner, entry.until)


# meiva:note
# The entry lowered to what still occurs, metadata kept: paying a debt down does not hand it to
# someone else.
def _lowered(entry: DebtEntry, found: int) -> DebtEntry:
    return DebtEntry(min(entry.count, found), entry.owner, entry.until)


def _plan_baseline_write(
    repo: Checkout, owner: str, before: str, table: DebtTable
) -> tuple[EditPlan, str | None]:
    rel = manifest_path(owner)
    path = repo.root / rel
    after = set_debt(before, table)
    if after == before:
        return EditPlan(), None
    written = {
        c.value: {key: entry_value(entry) for key, entry in rows.items()}
        for c, rows in table.items()
        if rows
    }
    if tomllib.loads(after).get(TABLE_KEY, {}) != written:
        raise MeizelError(f"{rel}: the [debt] tables cannot be rewritten; edit by hand")
    if after.strip():
        return EditPlan(writes={path: after}), rel
    return EditPlan(deletes=(path,)), rel


def _manifest_text(repo: Checkout, owner: str) -> str:
    return repo.load_text(manifest_path(owner), default="")


# meiva:note
# What a command says after writing manifests that were not there: the author has to stage them,
# because `check` reads tracked files only. `record` and `carry` both create them, so both say it
# here rather than each in its own words.
def created_notice(created: Sequence[str]) -> str | None:
    if not created:
        return None
    return (
        f"meizel: {len(created)} {'manifest' if len(created) == 1 else 'manifests'} created; "
        f"stage {'it' if len(created) == 1 else 'them'} with git add, "
        "because check reads tracked files only"
    )


def write_baseline(repo: Checkout, owner: str, table: DebtTable) -> str | None:
    """Rewrite the `[debt]` tables of `owner`'s manifest; the path written, or None if unchanged."""
    before = _manifest_text(repo, owner)
    plan, changed = _plan_baseline_write(repo, owner, before, table)
    repo.apply_plan(plan)
    return changed


def _new_back_edges(manifest: Manifest) -> set[tuple[str, str]]:
    """The fewest declared edges, beyond the recorded ones, to treat as back-edges so the graph is acyclic."""
    uses = manifest.graph()
    carried = set(manifest.back_edges())
    base = {src: [t for t in targets if (src, t) not in carried] for src, targets in uses.items()}
    chosen = carried | set(feedback_edges(base))
    return (chosen - set(unneeded_back_edges(uses, chosen))) - carried


def _cycle_facts(analysis: Analysis, manifest: Manifest) -> dict[str, int]:
    """Per `source -> target` key, the facts on the edges newly chosen as back-edges."""
    new = _new_back_edges(manifest)
    if not new:
        return {}
    seen: dict[tuple[str, int, str], str] = {}
    for proj in analysis.projections:
        if proj.exempt or proj.lca != manifest.directory or (proj.cf, proj.ct) not in new:
            continue
        diagnostic = proj.cycle_edge()
        if diagnostic.debt is not None:
            seen[(diagnostic.path, diagnostic.line, diagnostic.message)] = diagnostic.debt.key
    counts: dict[str, int] = {}
    for key in seen.values():
        counts[key] = counts.get(key, 0) + 1
    return counts


def _recordable(analysis: Analysis, targets: list[str]) -> list[Diagnostic]:
    return [d for d in analysis.diagnostics if d.debt is not None and _within(d.path, targets)]


def record(
    repo: Checkout, targets: list[str], jobs: int | None = None
) -> tuple[list[str], list[str], list[Diagnostic]]:
    """Record the violations under `targets`.

    Returns the manifests written, the ones among them that did not exist before, and every
    violation under `targets` that no baseline can hold.

    A count never goes down here (that is `prune`), and a violation outside `targets` is not
    recorded. The second result is every violation under `targets` that no baseline can hold.
    """
    analysis = analyze(repo.universe(), jobs=jobs)
    _refuse_malformed(analysis, "record")
    tables = {owner: _copy(m.debt) for owner, m in analysis.manifests.items()}
    for (owner, code, key), found in tally(_recordable(analysis, targets)).items():
        rows = tables.setdefault(owner, {}).setdefault(code, {})
        rows[key] = _raised(rows.get(key), len(found))
    for owner, manifest in analysis.manifests.items():
        if _within(owner, targets):
            facts = _cycle_facts(analysis, manifest)
            if facts:
                edges = tables[owner].setdefault(CYCLE_EDGE, {})
                for key, count in facts.items():
                    edges[key] = _raised(edges.get(key), count)
    written: list[str] = []
    created: list[str] = []
    for owner, table in sorted(tables.items()):
        if path := write_baseline(repo, owner, table):
            written.append(path)
            if owner not in analysis.manifests:
                created.append(path)
    unrecordable = [
        d
        for d in analysis.diagnostics
        if d.debt is None
        and d.code not in {Code.MANIFEST_CYCLE, Code.DEBT_PAID}
        and _within(d.path, targets)
    ]
    return written, created, unrecordable


def prune(repo: Checkout, jobs: int | None = None) -> list[str]:
    """Lower every baseline to what still occurs; returns a line per manifest rewritten."""
    analysis = analyze(repo.universe(), jobs=jobs)
    _refuse_malformed(analysis, "prune")
    found = tally(analysis.diagnostics)
    lines: list[str] = []
    for owner, manifest in sorted(analysis.manifests.items()):
        if not manifest.debt:
            continue
        unneeded = set(unneeded_back_edges(manifest.graph(), manifest.back_edges()))
        table: DebtTable = {}
        for code, rows in manifest.debt.items():
            kept = {
                key: _lowered(entry, len(found.get((owner, code, key), ())))
                for key, entry in rows.items()
                if not (code is CYCLE_EDGE and local_edge(key) in unneeded)
            }
            table[code] = {key: entry for key, entry in kept.items() if entry.count > 0}
        if write_baseline(repo, owner, table):
            lines.append(
                f"{manifest_path(owner)}: {total(manifest.debt)} recorded debts lowered to {total(table)}"
            )
    return lines


# meiva:note
# `debt resolve <manifest>...`: resolve the merge conflicts that sit inside the `[debt]` tables of the
# named manifests (see `resolve_debt_conflicts`), then lower every baseline to what still occurs.
# Recorded debt is derived from the tree, so a conflict between two recorded numbers is not a choice
# between two authors: both sides are kept, the lower count wins where both changed, and `prune`
# settles the rest. A conflict outside the debt tables, or a file that has none, refuses the whole
# command and writes nothing.
def resolve(repo: Checkout, paths: list[str], jobs: int | None = None) -> list[str]:
    plans: dict[Path, str] = {}
    problems: list[str] = []
    for rel in paths:
        if rel.rpartition("/")[2] != "meizel.toml":
            problems.append(f"{rel}: not a meizel.toml")
            continue
        text = _manifest_text(repo, rel.removesuffix("meizel.toml").rstrip("/"))
        resolved, found = resolve_debt_conflicts(text, rel)
        problems.extend(found)
        if resolved != text:
            plans[repo.root / rel] = resolved
    if problems:
        raise MeizelError("\n".join(problems))
    repo.apply_plan(EditPlan(writes=plans))
    lines = [
        f"meizel: resolved {rel}"
        for rel in sorted(path.relative_to(repo.root).as_posix() for path in plans)
    ]
    return [*lines, *prune(repo, jobs)]
