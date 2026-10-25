"""`sync --prune`: drop declared edges that no fact edge needs."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from meizel.allowances import POLICY, RETIRED_AMBIENT_SECTIONS, Policy, load_snapshot, parse
from meizel.contracts.analysis import Analysis, analyze
from meizel.contracts.debt_edit import prune as prune_debt
from meizel.contracts.declare import declare
from meizel.contracts.manifest import manifest_path
from meizel.contracts.manifest_text import ENTRY, HEADER, entry_end, items, unquote
from meizel.graph.model import MeizelError
from meizel.graph.universe import EditPlan, GitRepo

__all__ = ["plan_removals", "prepare_policy_snapshot", "prune", "remove_entries", "sync_manifests"]


def _rewrite_entry(lines: list[str], start: int, end: int, drop: set[str]) -> list[str]:
    match = ENTRY.match(lines[start])
    if match is None:
        raise MeizelError(f"line {start + 1}: not a manifest entry: {lines[start]!r}")
    block = "\n".join(lines[start : end + 1])
    kept = [item for item in items(block) if item not in drop]
    if not kept or "*" in drop:
        return []
    values = ", ".join(f'"{item}"' for item in kept)
    return [f"{match.group(1)}{match.group(2)} = [{values}]"]


_EXTERNAL_ENTRY = re.compile(r"""^\s*("[^"]*"|'[^']*'|[\w.-]+)\s*=\s*["']""")
_KEYED_ENTRY = re.compile(r"""^\s*("[^"]*"|'[^']*'|[\w.-]+)\s*=""")


# meiva:note
# The `key = ...` line of a section whose entries each take one line.
def _keyed_line(section: str, line: str) -> re.Match[str] | None:
    if section in {"state", "roots"}:
        return _KEYED_ENTRY.match(line)
    return _EXTERNAL_ENTRY.match(line) if section in {"external", "purity"} else None


def remove_entries(text: str, removals: dict[str, dict[str, set[str]]]) -> str:
    """Remove `targets` from entries of `[uses]`/`[loads]`; comments elsewhere survive."""
    lines = text.splitlines()
    out: list[str] = []
    section = ""
    index = 0
    while index < len(lines):
        header = HEADER.match(lines[index])
        if header:
            section = header.group(1)
        reason = _keyed_line(section, lines[index])
        if reason and unquote(reason.group(1)) in removals.get(section, {}):
            index += 1
            continue
        entry = ENTRY.match(lines[index])
        if entry and section in removals:
            end = entry_end(lines, index)
            drop = removals[section].get(unquote(entry.group(2)))
            out.extend(_rewrite_entry(lines, index, end, drop) if drop else lines[index : end + 1])
            index = end + 1
            continue
        out.append(lines[index])
        index += 1
    return _drop_empty_sections(out)


def _drop_empty_sections(lines: list[str]) -> str:
    kept: list[str] = []
    for position, line in enumerate(lines):
        if HEADER.match(line):
            rest = lines[position + 1 :]
            following = next((x for x in rest if x.strip() and not x.strip().startswith("#")), "")
            if not following or HEADER.match(following):
                continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    return (
        "\n".join(kept) + "\n"
        if any(x.strip() and not x.strip().startswith("#") for x in kept)
        else ""
    )


Removals = dict[str, dict[str, dict[str, set[str]]]]


def _collect(analysis: Analysis) -> tuple[Removals, list[str]]:
    """What to remove from each manifest, and a line for each removal."""
    removals: Removals = {}
    changes: list[str] = []
    for directory, cf, ct in analysis.stale_edges:
        removals.setdefault(directory, {}).setdefault("uses", {}).setdefault(cf, set()).add(ct)
        changes.append(f"{manifest_path(directory)}: removed {cf} -> {ct}")
    for directory, name in analysis.stale_loads:
        removals.setdefault(directory, {}).setdefault("loads", {})[name] = {"*"}
        changes.append(f"{manifest_path(directory)}: removed [loads] {name}")
    for directory, child in analysis.stale_exports:
        removals.setdefault(directory, {}).setdefault("", {}).setdefault("exports", set()).add(
            child
        )
        changes.append(f"{manifest_path(directory)}: removed [exports] {child}")
    for directory, manifest in analysis.manifests.items():
        for section, key, target in manifest.dangling:
            table = removals.setdefault(directory, {}).setdefault(
                "" if section == "exports" else section, {}
            )
            table.setdefault(key or "exports", set()).add(target if target is not None else "*")
            arrow = " -> " if key and target else ""
            changes.append(
                f"{manifest_path(directory)}: removed [{section}] {key or ''}{arrow}{target or ''}"
                " (not a child any more)"
            )
    return removals, changes


def plan_removals(root: Path, texts: Mapping[str, str], removals: Removals) -> EditPlan:
    writes: dict[Path, str] = {}
    deletes: list[Path] = []
    for relative, sections in removals.items():
        path = root / relative
        before = texts[relative]
        updated = remove_entries(before, sections)
        if updated == before:
            raise MeizelError(
                f"{path.relative_to(root)}: cannot remove {sections}; edit it by hand"
            )
        if updated:
            writes[path] = updated
        else:
            deletes.append(path)
    return EditPlan(writes=writes, deletes=tuple(deletes))


def _apply(root: Path, removals: Removals) -> None:
    repo = GitRepo(root)
    texts = {relative: repo.load_text(relative) for relative in removals}
    plan_removals(root, texts, removals).apply()


def prune(root: Path, jobs: int | None = None) -> list[str]:
    """Remove stale declarations until none remain; returns a line per removal."""
    changes: list[str] = []
    while True:
        removals, found = _collect(analyze(root, jobs=jobs))
        if not removals:
            return changes
        changes.extend(found)
        _apply(root, {manifest_path(d): sections for d, sections in removals.items()})


def sync_manifests(
    root: Path, targets: list[str] | None, jobs: int | None, *, do_declare: bool
) -> tuple[list[str], list[str]]:
    lines = list(prune(root, jobs))
    if not do_declare:
        return lines, []
    lines.extend(prune_debt(root, jobs))
    declared, left = declare(root, targets, jobs)
    lines.extend(declared)
    return lines, left


# meiva:note
# Normalize only a disposable historical checkout. Decode first, so malformed or unknown input
# fails before any write. Retired metadata remains in the returned Policy for migration and diff;
# every surviving policy entry and its reason keeps its original text for the current scanner.
def prepare_policy_snapshot(root: Path) -> Policy:
    try:
        policy = load_snapshot(root)
    except ValueError as error:
        raise MeizelError(f"cannot read the manifests of snapshot: {error}") from error
    text = "\n".join(policy.lines) + ("\n" if policy.lines else "")
    removals = {"ambient-state": {key: {"*"} for key in RETIRED_AMBIENT_SECTIONS}}
    current = remove_entries(text, removals)
    _, remaining = parse(current)
    if remaining:
        raise MeizelError(f"cannot normalize {POLICY} snapshot: {remaining[0]}")
    if current != text:
        _apply(root, {POLICY: removals})
    return policy
