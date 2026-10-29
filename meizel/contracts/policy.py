# meiva:note
# `meizel.policy.toml` in the checker: validation, rendering, `policy diff` and the surface report.
# The format and its reader live in `meizel.allowances`, shared with the lints whose allowances the
# file also holds. Here: a manifest may use a dynamic-load or external-code form only for the sites
# the policy lists (the free-text reason in a manifest no longer counts), a change to the file is
# reported by `policy diff` so a reviewer sees every widening at once, and `policy surface` reports
# the declarations of both trees - the policy and each manifest's `[roots]`/`[state]` - so that an
# allowance that appears is never silent either.

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypeGuard

from meizel.allowances import NAMES, PER_NAME, POLICY, REASONS, RULES, Policy, parse, parse_snapshot
from meizel.contracts.manifest import Manifest, load_manifests
from meizel.graph.model import Code, Diagnostic, MeizelError
from meizel.graph.universe import Universe

__all__ = [
    "POLICY",
    "Change",
    "Item",
    "Policy",
    "Surface",
    "changes",
    "check",
    "load_policy",
    "policy_at",
    "render_policy",
]


def parse_policy(text: str) -> tuple[Policy, list[Diagnostic]]:
    policy, problems = parse(text)
    return policy, [Diagnostic(POLICY, 1, Code.POLICY_SYNTAX, message) for message in problems]


def load_policy(universe: Universe, text: str | None = None) -> tuple[Policy, list[Diagnostic]]:
    """The policy of the working tree; an absent file is an empty one, so nothing is allowed."""
    if text is not None:
        return parse_policy(text)
    if POLICY not in universe.files:
        return Policy(), []
    return parse_policy(universe.repo.load_text(POLICY))


def _site(manifest: Manifest, name: str) -> str:
    return f"{manifest.directory}/{name}" if manifest.directory else name


def check(manifests: dict[str, Manifest], policy: Policy) -> list[Diagnostic]:
    """Every `[external]` and `[loads]` site in a manifest that the policy does not list."""
    found: list[Diagnostic] = []
    for _, manifest in sorted(manifests.items()):
        for name in manifest.external:
            if _site(manifest, name) not in policy.external:
                found.append(_unlisted(manifest, name, "external", ""))
        for name, targets in manifest.loads.items():
            listed = policy.loads.get(_site(manifest, name), ())
            missing = [t for t in targets if t not in listed]
            if missing:
                found.append(_unlisted(manifest, name, "loads", ", ".join(missing)))
    return found


def _unlisted(manifest: Manifest, name: str, table: str, detail: str) -> Diagnostic:
    site = _site(manifest, name)
    what = f" -> {detail}" if detail else ""
    return Diagnostic(
        manifest.path,
        manifest.line_of(name),
        Code.UNLISTED_ALLOWANCE,
        f"[{table}] {site}{what} is not listed in {POLICY}",
        (
            f"a [{table}] allowance is a decision, not an edit: it exists only where {POLICY} lists it, "
            "and a reason written in the manifest does not count; rewrite the code as a static "
            "import, or ask the maintainers to list the site",
        ),
    )


def render_policy(external: dict[str, str], loads: dict[str, tuple[str, ...]]) -> str:
    """A policy file listing exactly these sites, sorted."""
    blocks = [
        "[external]\n"
        + "".join(f"{json.dumps(p)} = {json.dumps(r)}\n" for p, r in sorted(external.items()))
    ]
    blocks.append(
        "[loads]\n"
        + "".join(
            f"{json.dumps(p)} = [{', '.join(json.dumps(t) for t in targets)}]\n"
            for p, targets in sorted(loads.items())
        )
    )
    blocks.append("[default-arguments]\n")
    return "\n".join(blocks)


def policy_at(universe: Universe, ref: str) -> Policy:
    """The policy at a git ref; empty when the ref has none."""
    raw = universe.repo.show_optional_text(ref, POLICY)
    if raw is None:
        return Policy()
    policy, problems = parse_snapshot(raw)
    if problems:
        raise MeizelError(f"{ref}:{POLICY}: {problems[0]}")
    return policy


def _is_object_tuple(value: object) -> TypeGuard[tuple[object, ...]]:
    return isinstance(value, tuple)


def changes(old: Policy, new: Policy) -> list[str]:
    """A line per entry added, removed or changed between two policies."""
    lines: list[str] = []

    def text(value: object) -> str:
        if _is_object_tuple(value):
            return "[" + ", ".join(str(item) for item in value) + "]"
        return str(value)

    def show(key: str, value: object) -> str:
        return f"{key} = {text(value)}" if text(value) else key

    def table(name: str, before: dict[str, object], after: dict[str, object]) -> None:
        lines.extend(
            f"+ [{name}] {show(k, after[k])}" for k in sorted(after.keys() - before.keys())
        )
        lines.extend(
            f"- [{name}] {show(k, before[k])}" for k in sorted(before.keys() - after.keys())
        )
        lines.extend(
            f"~ [{name}] {k}: {text(before[k])} -> {text(after[k])}"
            for k in sorted(before.keys() & after.keys())
            if before[k] != after[k]
        )

    table("external", dict(old.external), dict(new.external))
    table("loads", dict(old.loads), dict(new.loads))
    table(
        "default-arguments",
        dict.fromkeys(old.default_arguments, ""),
        dict.fromkeys(new.default_arguments, ""),
    )
    for rule, section in sorted(old.listed.keys() | new.listed.keys()):
        table(
            f"{rule}.{section}",
            dict(old.listed.get((rule, section), {})),
            dict(new.listed.get((rule, section), {})),
        )
    for rule, section in sorted(old.unless.keys() | new.unless.keys()):
        table(
            f"{rule}.{section} unless",
            {
                name: f"unless {parameter}"
                for name, parameter in old.unless.get((rule, section), {}).items()
            },
            {
                name: f"unless {parameter}"
                for name, parameter in new.unless.get((rule, section), {}).items()
            },
        )
    for rule, section in sorted(old.named.keys() | new.named.keys()):
        table(
            f"{rule}.{section}",
            dict.fromkeys(old.named.get((rule, section), ()), ""),
            dict.fromkeys(new.named.get((rule, section), ()), ""),
        )
    return lines


# meiva:note
# The change types a surface report uses, and the two directions it names. A change that narrows
# the surface is one the checker stops reporting; one that widens it is a site it reports again.
# `moved` is one declaration that followed its file in git's own rename, which is neither.
ADDED = "added"
REMOVED = "removed"
MODIFIED = "modified"
MOVED = "moved"
NARROWER = "narrower"
WIDER = "wider"
UNKNOWN = "unknown"

# meiva:note
# The tables whose entries excuse a site: `meizel.policy.toml`'s three checker tables, a manifest's
# `[roots]` and `[state]`, and an `unless` clause. Every other table holds a rule's vocabulary - the
# names it treats as a read of the world - where an entry that appears shows the checker more.
_ALLOWANCES = frozenset({"external", "loads", "default-arguments", "roots", "state"})
_UNLESS = " unless"

# meiva:note
# The two directions that are named, in the order the report prints them, with the one line that
# says what the direction means for the change below it.
_DIRECTIONS = (
    (NARROWER, "Narrower", "the checker stops reporting a site it used to"),
    (WIDER, "Wider", "the checker reports a site it did not"),
    (
        UNKNOWN,
        "Direction unknown",
        "the change is real and the table's own format does not say which way it points",
    ),
)


# meiva:note
# A declaration that decides what the checker reports: an entry of `meizel.policy.toml`, or a file
# that a manifest's `[roots]` or `[state]` lets read the world. `source` is the file that holds it,
# `table` the table inside that file, and `value` the part that would have to change for the entry
# to mean something else: the reason, the kinds, or the `unless` parameter.
@dataclass(frozen=True, slots=True)
class Item:
    source: str
    table: str
    name: str
    value: str = ""

    # meiva:note
    # The entry as one line, the way `policy diff` writes it.
    def shown(self) -> str:
        return f"{self.name} = {self.value}" if self.value else self.name


# meiva:note
# One difference between two surfaces. `before` holds the name a move came from, or the value a
# modification replaced; `direction` is which way the change points for what the checker still
# reports, and stays `unknown` where the table's own format does not decide it.
@dataclass(frozen=True, slots=True)
class Change:
    kind: str
    source: str
    table: str
    name: str
    value: str = ""
    before: str = ""
    direction: str = UNKNOWN


# meiva:note
# The shape a table of the policy is written in, or None for a table this file does not know: a
# `name = reason` entry excuses something, a list names a vocabulary, and `ambient-readers` is the
# one vocabulary that writes itself with a reason.
def _shape(table: str) -> str | None:
    if table in _ALLOWANCES or table.endswith(_UNLESS):
        return REASONS
    rule, _, section = table.partition(".")
    if rule == "ambient-readers":
        return NAMES
    if rule in PER_NAME:
        return REASONS
    return RULES.get(rule, {}).get(section)


# meiva:note
# Which way one change points. An exemption that appears narrows what is reported and one that
# goes widens it; a vocabulary does the opposite. A value edit, and a table whose shape this file
# does not know, are `unknown`: a direction is only ever read off the table's own format, never
# guessed from the entry's name.
def _direction(table: str, kind: str) -> str:
    shape = _shape(table)
    if shape is None or kind not in (ADDED, REMOVED):
        return UNKNOWN
    narrows = (shape == REASONS) == (kind == ADDED)
    return NARROWER if narrows else WIDER


# meiva:note
# The ambient kinds a root or a state entry declares, as the manifest writes them back.
def _kinds(kinds: Sequence[object]) -> str:
    return "[" + ", ".join(str(getattr(kind, "value", kind)) for kind in kinds) + "]"


# meiva:note
# How one declaration is identified across two trees: its source file, its table and its name, so
# the same entry in two revisions meets itself.
Key = tuple[str, str, str]


# meiva:note
# The declarations of one policy: one item per entry, so a list section loses exactly the names it
# dropped and a `[loads]` entry gains exactly the directory it added.
def _policy_items(policy: Policy) -> list[Item]:
    found = [Item(POLICY, "external", path, reason) for path, reason in policy.external.items()]
    found.extend(
        Item(POLICY, "loads", f"{path} -> {target}", "")
        for path, targets in policy.loads.items()
        for target in targets
    )
    found.extend(Item(POLICY, "default-arguments", name) for name in policy.default_arguments)
    found.extend(
        Item(POLICY, f"{rule}.{section}", name, reason)
        for (rule, section), entries in policy.listed.items()
        for name, reason in entries.items()
    )
    found.extend(
        Item(POLICY, f"{rule}.{section} unless", name, f"unless {parameter}")
        for (rule, section), parameters in policy.unless.items()
        for name, parameter in parameters.items()
    )
    found.extend(
        Item(POLICY, f"{rule}.{section}", name)
        for (rule, section), names in policy.named.items()
        for name in names
    )
    return found


# meiva:note
# One side of a comparison as change records, minus everything a rename already paired: the entries
# this tree declares and the other does not, each carrying the direction its own table's format gives.
def _one_side(items: Mapping[Key, Item], taken: set[Key], kind: str) -> list[Change]:
    return [
        Change(
            kind,
            item.source,
            item.table,
            item.name,
            item.value,
            direction=_direction(item.table, kind),
        )
        for key, item in sorted(items.items())
        if key not in taken
    ]


# meiva:note
# An entry both sides declare with different text: still there, only what it says changed, which is
# why its direction is unknown rather than read off a table's format.
def _edited(mine: Mapping[Key, Item], theirs: Mapping[Key, Item]) -> list[Change]:
    return [
        Change(
            MODIFIED,
            theirs[key].source,
            theirs[key].table,
            theirs[key].name,
            theirs[key].value,
            before=mine[key].value,
        )
        for key in sorted(mine.keys() & theirs.keys())
        if mine[key].value != theirs[key].value
    ]


# meiva:note
# The declarations of one manifest: `[roots]` and `[state]` are the two tables that let a file read
# without the checker saying so. The `why` is prose and changes nothing about what is seen, so the
# value is the kinds alone.
def _manifest_items(manifest: Manifest) -> list[Item]:
    found = [
        Item(manifest.path, "roots", name, _kinds(root.kinds))
        for name, root in manifest.roots.items()
    ]
    found.extend(
        Item(manifest.path, "state", name, _kinds(kinds)) for name, kinds in manifest.state.items()
    )
    return found


# meiva:note
# The path an entry names, where its name is one: a `[roots]`/`[state]` file below its own
# directory, an `[external]` site, or the source of a `[loads]` entry. A dotted name or a `path::site`
# is not a path, so a rename cannot pair it and it is reported as added and removed.
def _path(item: Item) -> str | None:
    if item.table in {"roots", "state"}:
        directory = item.source.rpartition("/")[0]
        return f"{directory}/{item.name}" if directory else item.name
    if item.table == "external":
        return item.name
    if item.table == "loads":
        return item.name.partition(" -> ")[0]
    return None


# meiva:note
# The declarations that followed a file git saw renamed, and the keys they consumed on both sides.
# A pair needs the same table and the same value at the end of git's own rename: an entry that also
# changed while it moved is reported as a removal and an addition, not as a move.
def _moved(
    gone: Mapping[tuple[str, str, str], Item],
    came: Mapping[tuple[str, str, str], Item],
    renames: Mapping[str, str],
) -> tuple[list[Change], set[tuple[str, str, str]]]:
    found: list[Change] = []
    taken: set[tuple[str, str, str]] = set()
    index: dict[tuple[str, str, str], tuple[str, str, str]] = {}
    for key, item in sorted(came.items()):
        path = _path(item)
        if path is not None:
            index.setdefault((item.table, item.value, path), key)
    for key, item in sorted(gone.items()):
        path = _path(item)
        target = (
            index.get((item.table, item.value, renames[path]))
            if path is not None and path in renames
            else None
        )
        if target is None or target in taken:
            continue
        taken.update((key, target))
        arrived = came[target]
        found.append(
            Change(
                MOVED,
                arrived.source,
                arrived.table,
                arrived.name,
                arrived.value,
                before=item.name,
            )
        )
    return found, taken


# meiva:note
# What one tree lets the checker see, and what of it could not be read. `unread` holds one
# `(path, why)` per declaration this tree could not be read at all: those are dropped from both
# sides of a comparison and reported as unknown, because a source that cannot be parsed is not a
# source that declares nothing.
@dataclass(frozen=True, slots=True)
class Surface:
    items: tuple[Item, ...] = ()
    unread: tuple[tuple[str, str], ...] = ()

    # meiva:note
    # One tree's surface, read by the policy's and the manifests' own readers: every declaration it
    # makes, and the sources this tree could not read at all.
    @classmethod
    def of(cls, universe: Universe, *, historical: bool = False) -> Surface:
        found: list[Item] = []
        unread: list[tuple[str, str]] = []
        if POLICY in universe.files:
            text = universe.repo.load_text(POLICY)
            policy, problems = parse_snapshot(text) if historical else parse(text)
            unread.extend((POLICY, message) for message in problems)
            found.extend(_policy_items(policy))
        manifests, diagnostics = load_manifests(universe)
        unread.extend(
            (diagnostic.path, f"line {diagnostic.line}: {diagnostic.message}")
            for diagnostic in diagnostics
        )
        broken = {diagnostic.path for diagnostic in diagnostics}
        found.extend(
            item
            for _, manifest in sorted(manifests.items())
            if manifest.path not in broken
            for item in _manifest_items(manifest)
        )
        return cls(tuple(found), tuple(unread))

    # meiva:note
    # Every declaration the two surfaces differ by, paired through git's renames.
    def changes(self, other: Surface, renames: Mapping[str, str] | None = None) -> list[Change]:
        skip = {source for source, _ in (*self.unread, *other.unread)}
        mine = {(i.source, i.table, i.name): i for i in self.items if i.source not in skip}
        theirs = {(i.source, i.table, i.name): i for i in other.items if i.source not in skip}
        gone = {key: mine[key] for key in mine.keys() - theirs.keys()}
        came = {key: theirs[key] for key in theirs.keys() - mine.keys()}
        moved, taken = _moved(gone, came, dict(renames or {}))
        return [
            *moved,
            *_one_side(gone, taken, REMOVED),
            *_one_side(came, taken, ADDED),
            *_edited(mine, theirs),
        ]

    def report(
        self, other: Surface, renames: Mapping[str, str] | None = None, *, against: str
    ) -> str:
        changes = self.changes(other, renames)
        unread = (*self.unread, *other.unread)
        body = [
            f"### Detection surface against `{against}`: {_headline(changes, unread)}",
            "",
            "`meizel.policy.toml` and every `meizel.toml`'s `[roots]` and `[state]` decide what the "
            "checker still reports. A line here is listed and never required: nothing in this "
            "block fails a run. In a Trunk batch the tree also carries the batches queued ahead of "
            "this one, so a line may belong to one of them; the member pull requests are the record "
            "of who changed it.",
            "",
        ]
        body.extend(_counts(changes))
        if unread:
            body.extend(["**Could not be read**, so nothing is claimed about it:", ""])
            body.extend(f"- `{source}`: {why}" for source, why in unread)
            body.append("")
        for direction, heading, gloss in _DIRECTIONS:
            listed = [
                change
                for change in changes
                if change.direction == direction and change.kind != MOVED
            ]
            if listed:
                body.extend([f"**{heading}** - {gloss}:", ""])
                body.extend(_line(change) for change in listed)
                body.append("")
        moved = [change for change in changes if change.kind == MOVED]
        if moved:
            body.extend(
                [
                    "**Moved** - one declaration followed its file in git's rename, not a removal "
                    "and an addition:",
                    "",
                    *(_line(change) for change in moved),
                    "",
                ]
            )
        return "\n".join(body).rstrip() + "\n"


# meiva:note
# The one line at the top of the report: how many changes point each way. A run that could not read
# a source says so rather than "no change", because a source that did not parse declares nothing
# only in this tool's eyes.
def _headline(changes: Sequence[Change], unread: Sequence[tuple[str, str]]) -> str:
    counted = Counter(change.direction for change in changes if change.kind != MOVED)
    parts = [
        f"{counted[direction]} {direction}" for direction, _, _ in _DIRECTIONS if counted[direction]
    ]
    moved = sum(1 for change in changes if change.kind == MOVED)
    if moved:
        parts.append(f"{moved} moved")
    if unread:
        parts.append(f"{len(unread)} unread")
    return ", ".join(parts) if parts else "no change"


# meiva:note
# The counts, one row per file and table: the aggregate a reviewer reads before anything else.
def _counts(changes: Sequence[Change]) -> list[str]:
    rows: dict[tuple[str, str], Counter[str]] = {}
    for change in changes:
        rows.setdefault((change.source, change.table), Counter())[change.kind] += 1
    if not rows:
        return []
    lines = [
        "| source | table | added | removed | modified | moved |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    lines.extend(
        f"| `{source}` | `{table}` | {counts[ADDED]} | {counts[REMOVED]} | "
        f"{counts[MODIFIED]} | {counts[MOVED]} |"
        for (source, table), counts in sorted(rows.items())
    )
    return [*lines, ""]


# meiva:note
# One change as one line: what happened, where the declaration lives, and what it now says.
def _line(change: Change) -> str:
    where = f"- **{change.kind}** `{change.source} [{change.table}]` `{change.name}`"
    if change.kind == MODIFIED:
        return f"{where}: `{change.before}` -> `{change.value}`"
    if change.kind == MOVED:
        return f"{where} was `{change.before}`"
    return f"{where} = {change.value}" if change.value else where
