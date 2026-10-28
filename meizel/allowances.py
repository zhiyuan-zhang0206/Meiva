# meiva:note
# `meizel.policy.toml` is the one closed list of allowances, so a declaration that disappears is a
# narrowing of what the checker still reports, and every comparison against the base stays green
# while it happens. This module owns the file's format and reader; the declarations a change takes
# away, and the `Policy-Removal` line that answers them, belong to the same owner for that reason.
# meiva:link meizel/docs/checker/policy.meiva.okf.md


from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import TypeGuard, assert_never

from meizel.checkout import GitRepo
from meizel.graph.model import AmbientKind

__all__ = [
    "POLICY",
    "RETIRED_AMBIENT_SECTIONS",
    "RULES",
    "Declaration",
    "Policy",
    "Removal",
    "RemovalReport",
    "invalidate_cache",
    "load",
    "load_snapshot",
    "parse",
    "parse_snapshot",
    "removal_report",
]

POLICY = "meizel.policy.toml"
CHECKER_TABLES = ("external", "loads", "default-arguments")

REASONS = "reasons"  # a table of `name = "reason"`
NAMES = "names"  # a list of names

# rule -> section -> shape. A rule listed in `PER_NAME` has one reasons table per name it governs.
RULES: dict[str, dict[str, str]] = {
    "ambient": {"settings": NAMES, "db": NAMES, "endpoints": NAMES, "bus": NAMES},
    "ambient-readers": dict.fromkeys((kind.value for kind in AmbientKind), REASONS),
    "ambient-state": {
        "sink-callees": NAMES,
        "sink-callee-suffixes": NAMES,
        "wiring-callees": NAMES,
        "pure-callees": NAMES,
        "pure-chain-callees": NAMES,
        "skill-script-patterns": NAMES,
        "config-modules": NAMES,
        "config-readers": NAMES,
        "sink-facades": REASONS,
        "pure-repo-callees": REASONS,
        "sites": REASONS,
    },
    "clock-lattice": {"family-modules": NAMES, "independent-clocks": REASONS},
    "no-os-environ": {
        "test-only-dirs": NAMES,
        "rule-inputs": NAMES,
        "provider-key-vars": REASONS,
        "live-read-vars": REASONS,
        "files": REASONS,
    },
    "path-imports": {"scope": NAMES, "skills-scope": NAMES},
    "python-commands": {"argv-wrappers": NAMES},
    "turn-scoped-config": {
        "scan-dirs": NAMES,
        "extra-files": NAMES,
        "rule-inputs": NAMES,
        "files": REASONS,
        "plugin-mechanism-files": REASONS,
    },
    "type-checking": {"files": REASONS},
    "approved-patches": {"files": REASONS},
    "single-owner": {"postgres-dial-owners": NAMES},
    "purity": {
        "impure-imports": NAMES,
        "impure-calls": NAMES,
        "impure-fixtures": NAMES,
        "max-uses": NAMES,
        "max-exports": NAMES,
        "max-public-symbols": NAMES,
        "max-params": NAMES,
        "max-attr-hops": NAMES,
    },
    "typescript": {
        "scopes": NAMES,
        "import-sources": NAMES,
        "aliases": NAMES,
        "extensions": NAMES,
        "impure-calls": NAMES,
        "impure-hooks": NAMES,
    },
}
PER_NAME = frozenset({"single-owner"})


# meiva:note
# These five lists belonged to the replaced ambient registries. Only an explicit historical
# snapshot decoder accepts them; the live format continues to reject every retired section.
RETIRED_AMBIENT_SECTIONS = frozenset(
    {"db-modules", "db-primitives", "db-handles", "bus-handles", "endpoint-handles"}
)


class _PolicySource(StrEnum):
    CURRENT = "current"
    SNAPSHOT = "snapshot"


def _sections(rule: str, source: _PolicySource) -> dict[str, str]:
    if source is _PolicySource.CURRENT:
        return RULES[rule]
    if source is _PolicySource.SNAPSHOT:
        return (
            RULES[rule] | dict.fromkeys(RETIRED_AMBIENT_SECTIONS, NAMES)
            if rule == "ambient-state"
            else RULES[rule]
        )
    assert_never(source)


@dataclass(frozen=True, slots=True)
class Policy:
    external: dict[str, str] = field(default_factory=dict[str, str])
    loads: dict[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    default_arguments: tuple[str, ...] = ()
    listed: dict[tuple[str, str], dict[str, str]] = field(
        default_factory=dict[tuple[str, str], dict[str, str]]
    )
    named: dict[tuple[str, str], tuple[str, ...]] = field(
        default_factory=dict[tuple[str, str], tuple[str, ...]]
    )
    lines: list[str] = field(default_factory=list[str])

    def reasons(self, rule: str, section: str) -> dict[str, str]:
        """The `name -> reason` entries of one section; empty when the file has none."""
        self._known(rule, section, REASONS)
        return dict(self.listed.get((rule, section), {}))

    # meiva:note
    # The line of the first uncommented line that quotes `name`, within `[table]` when one is given;
    # 1 when none does.
    def line_of(self, name: str, table: str | None = None) -> int:
        quoted = f'"{name}"'
        inside = table is None
        for number, text in enumerate(self.lines, 1):
            if text.startswith("["):
                inside = table is None or text.strip() == f"[{table}]"
            if (
                inside
                and quoted in text
                and not text.lstrip().startswith("#")
                and not text.startswith("[")
            ):
                return number
        return 1

    def names(self, rule: str, section: str) -> tuple[str, ...]:
        """The names of one list section; empty when the file has none."""
        self._known(rule, section, NAMES)
        return self.named.get((rule, section), ())

    @staticmethod
    def _known(rule: str, section: str, shape: str) -> None:
        wanted = (
            RULES[rule].get(section, REASONS)
            if rule in PER_NAME
            else RULES.get(rule, {}).get(section)
        )
        if wanted != shape:
            raise KeyError(f"{POLICY} has no {shape} section [{rule}] {section}")


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_table(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(k, str) for k in value)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_str_list(value: object) -> TypeGuard[list[str]]:
    return _is_object_list(value) and all(isinstance(item, str) for item in value)


def _reasons(table: object, where: str, problems: list[str]) -> dict[str, str]:
    if not _is_table(table):
        problems.append(f"{where} must be a table of `name = reason`")
        return {}
    found: dict[str, str] = {}
    for name, reason in table.items():
        if isinstance(reason, str) and reason.strip():
            found[name] = reason.strip()
        else:
            problems.append(f"{where} {name!r} needs a non-empty reason string")
    return found


def _names(value: object, where: str, problems: list[str]) -> tuple[str, ...]:
    if not _is_str_list(value) or not all(value):
        problems.append(f"{where} must be a list of non-empty strings")
        return ()
    return tuple(value)


def _rule(
    rule: str,
    table: object,
    listed: dict[tuple[str, str], dict[str, str]],
    named: dict[tuple[str, str], tuple[str, ...]],
    problems: list[str],
    source: _PolicySource,
) -> None:
    if not _is_table(table):
        problems.append(f"[{rule}] must be a table")
        return
    sections = _sections(rule, source)
    for section, value in table.items():
        shape = sections.get(section, REASONS) if rule in PER_NAME else sections.get(section)
        where = f"[{rule}] {section}" if shape == NAMES else f"[{rule}.{section}]"
        if shape == REASONS:
            listed[(rule, section)] = _reasons(value, where, problems)
        elif shape == NAMES:
            if (
                rule == "purity"
                and section.startswith("max-")
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 1
            ):
                named[(rule, section)] = (str(value),)
            else:
                named[(rule, section)] = _names(value, where, problems)
        else:
            known = ", ".join(sorted(sections))
            problems.append(f"[{rule}] has no section {section!r}; the sections are {known}")


def _checker(
    document: dict[str, object], problems: list[str]
) -> tuple[dict[str, str], dict[str, tuple[str, ...]], tuple[str, ...]]:
    external = document.get("external", {})
    loads = document.get("loads", {})
    defaults = document.get("default-arguments", {})
    if not (_is_table(external) and _is_table(loads) and _is_table(defaults)):
        problems.append("every table must be a table")
        return {}, {}, ()
    parsed_external = _reasons(external, "[external]", problems)
    parsed_loads: dict[str, tuple[str, ...]] = {}
    for path, targets in loads.items():
        if _is_str_list(targets):
            parsed_loads[path] = tuple(targets)
        else:
            problems.append(f"[loads] {path!r} must be a list of directories")
    return parsed_external, parsed_loads, tuple(defaults)


# meiva:note
# Both entry points use the same shape checks. Historical input adds only the five retired
# NAMES lists, retained in Policy.named so policy diff and baseline migration can still see them.
def _parse(text: str, source: _PolicySource) -> tuple[Policy, list[str]]:
    lines = text.splitlines()
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        return Policy(lines=lines), [f"invalid TOML: {error}"]
    problems: list[str] = []
    for name in document:
        if name not in CHECKER_TABLES and name not in RULES:
            tables = ", ".join((*CHECKER_TABLES, *RULES))
            problems.append(f"unknown table {name!r}; the tables are {tables}")
    external, loads, default_arguments = _checker(document, problems)
    listed: dict[tuple[str, str], dict[str, str]] = {}
    named: dict[tuple[str, str], tuple[str, ...]] = {}
    for rule in RULES:
        if rule in document:
            _rule(rule, document[rule], listed, named, problems, source)
    return (
        Policy(
            external=external,
            loads=loads,
            default_arguments=default_arguments,
            listed=listed,
            named=named,
            lines=lines,
        ),
        problems,
    )


# meiva:note
# Current working-tree policy never accepts the retired ambient registry vocabulary.
def parse(text: str) -> tuple[Policy, list[str]]:
    return _parse(text, _PolicySource.CURRENT)


# meiva:note
# A Git snapshot may use the immediately preceding registry format. Unknown tables, unknown
# sections, invalid TOML and invalid values still fail through the common parser.
def parse_snapshot(text: str) -> tuple[Policy, list[str]]:
    return _parse(text, _PolicySource.SNAPSHOT)


def _load(root: Path, source: _PolicySource) -> Policy:
    repo = GitRepo(root)
    if not repo.has_file(POLICY):
        return Policy()
    policy, problems = _parse(repo.load_text(POLICY), source)
    if problems:
        raise ValueError(f"{POLICY}: {problems[0]}")
    return policy


# meiva:note
# The live policy never accepts the retired snapshot vocabulary. An absent file lists nothing;
# malformed input fails through the shared policy reader.
@cache
def load(root: Path) -> Policy:
    return _load(root, _PolicySource.CURRENT)


# meiva:note
# Historical consumers share the policy owner's absent-file and input validation boundary.
# The explicit entry never relaxes live parsing or caches a mutable temporary snapshot.
def load_snapshot(root: Path) -> Policy:
    return _load(root, _PolicySource.SNAPSHOT)


# meiva:note
# Mutable comparison snapshots replace their policy after validating the old input. Cache
# invalidation belongs to this reader and runs only once the snapshot mutation has completed.
def invalidate_cache() -> None:
    load.cache_clear()


# meiva:note
# One entry of the policy: the table that holds it, its name, and how the file writes it. `named` is
# the one string a `Policy-Removal` line names it with.
@dataclass(frozen=True, slots=True)
class Declaration:
    table: str
    entry: str
    shown: str

    @property
    def named(self) -> str:
        return f"{self.table}.{self.entry}"


# meiva:note
# One `Policy-Removal` line: what it names, why the declaration went, and what covers it now.
@dataclass(frozen=True, slots=True)
class Removal:
    declared: str
    why: str
    replacement: str


# meiva:note
# What a change took away from the policy, and what the record says about it. `unanswered` is the
# failure this exists for; `unused` is a line that names a declaration the change did not remove — a
# record of something else, never a failure; `problems` is one entry per line of the record that
# opens with the key and is not three fields, so a typo cannot pass for an answer.
@dataclass(frozen=True, slots=True)
class RemovalReport:
    removed: tuple[Declaration, ...]
    added: tuple[Declaration, ...]
    declared: tuple[Removal, ...]
    unanswered: tuple[Declaration, ...]
    unused: tuple[Removal, ...]
    problems: tuple[str, ...]

    # meiva:note
    # The lines that would answer `unanswered`: the entry alone, or the table that lost several.
    def templates(self) -> tuple[str, ...]:
        grouped: dict[str, list[Declaration]] = {}
        for item in self.unanswered:
            grouped.setdefault(item.table, []).append(item)
        lines: list[str] = []
        for table, items in sorted(grouped.items()):
            named = table if len(items) > 1 else items[0].named
            lines.append(
                f"Policy-Removal: {named}{_SEPARATOR}<why it is no longer needed>"
                f"{_SEPARATOR}<what replaces it, or the gap it leaves>"
            )
        return tuple(lines)


_KEY = re.compile(r"^[ \t]*policy-removal[ \t]*:[ \t]*(?P<rest>\S.*?)[ \t]*$", re.IGNORECASE)
_SEPARATOR = " \u2014 "
_PLACEHOLDER = re.compile(r"<[^<>]+>")
_MIN_FIELD = 8


# meiva:note
# Every declaration the policy makes, keyed by the string a line names it with. The four checker
# tables hold one declaration per entry; a list section holds one per name, so a shortened list loses
# exactly the names it dropped and a deleted section loses all of them.
def _declarations(policy: Policy) -> dict[str, Declaration]:
    found: dict[str, Declaration] = {}

    def keep(table: str, entry: str, shown: str) -> None:
        item = Declaration(table, entry, shown)
        found[item.named] = item

    for path, reason in policy.external.items():
        keep("external", path, f"{path} = {reason}")
    for path, targets in policy.loads.items():
        for target in targets:
            keep("loads", f"{path} -> {target}", f"{path} = [{target}]")
    for name in policy.default_arguments:
        keep("default-arguments", name, name)
    for (rule, section), entries in policy.listed.items():
        for name, reason in entries.items():
            keep(f"{rule}.{section}", name, f"{name} = {reason}")
    for (rule, section), names in policy.named.items():
        for name in names:
            keep(f"{rule}.{section}", name, name)
    return found


# meiva:note
# A line that names something but whose prose fields still hold the template, so it answers nothing.
@dataclass(frozen=True, slots=True)
class _Skeleton:
    declared: str
    line: str


# meiva:note
# The shape of one answer: `<table>[.<entry>] — <why> — <replacement or gap>`. A line with fewer
# than three fields is a problem rather than a non-answer, and git's own template comments are
# skipped as for any trailer. A line whose prose fields are still placeholders is neither: it is the
# shape quoted, not an attempt to answer (a pull request that explains the format must not fail it),
# and it answers nothing wherever it appears.
def _declared_removals(
    text: str,
) -> tuple[tuple[Removal, ...], tuple[_Skeleton, ...], tuple[str, ...]]:
    found: list[Removal] = []
    skeletons: list[_Skeleton] = []
    problems: list[str] = []
    for number, raw in enumerate(text.splitlines(), 1):
        if raw.lstrip().startswith("#"):
            continue
        match = _KEY.match(raw)
        if match is None:
            continue
        body = match.group("rest").strip()
        declared, first, remainder = body.partition(_SEPARATOR)
        why, second, replacement = remainder.partition(_SEPARATOR)
        nameless = not declared.strip() or declared.lstrip().startswith("\u2014")
        missing = (
            ["the `<table>[.<entry>]` name"]
            if nameless
            else [
                name
                for name, part in (
                    ("the `<table>[.<entry>]` name", declared),
                    ("the `<why it is no longer needed>` field", first),
                    ("the `<what replaces it, or the gap it leaves>` field", second),
                )
                if not part.strip()
            ]
        )
        if missing:
            problems.append(
                f"line {number} is not a declaration: it is missing {_and(missing)}. A "
                f"`Policy-Removal` line needs three fields, `-`-free and separated by an em dash: "
                f"`<table>[.<entry>] — <why it is no longer needed> — "
                f"<what replaces it, or the gap it leaves>`: {raw.strip()}"
            )
            continue
        if not _answered(why) or not _answered(replacement):
            skeletons.append(_Skeleton(declared.strip(), raw.strip()))
            continue
        found.append(Removal(declared.strip(), why.strip(), replacement.strip()))
    return tuple(found), tuple(skeletons), tuple(problems)


# meiva:note
# One name, two names with "and", three or more with commas and a closing "and".
def _and(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


# meiva:note
# A prose field is answered when it says something and is not a placeholder: `<why ...>` is the
# template, not a reason.
def _answered(text: str) -> bool:
    value = text.strip()
    return len(value) >= _MIN_FIELD and _PLACEHOLDER.search(value) is None


# meiva:note
# A line answers a declaration when it names it, or names a table or rule that contains it: an entry
# disappears on its own, a section with everything it held, or a rule with all its sections, and one
# line per name would turn a deleted section into a page of boilerplate.
def _answers(removal: Removal, declaration: Declaration) -> bool:
    return declaration.named == removal.declared or declaration.named.startswith(
        f"{removal.declared}."
    )


# meiva:note
# The declarations `new` dropped, and which of them the `record` answers, in one pass.
def removal_report(old: Policy, new: Policy, record: str) -> RemovalReport:
    before = _declarations(old)
    after = _declarations(new)
    removed = tuple(before[key] for key in sorted(before.keys() - after.keys()))
    added = tuple(after[key] for key in sorted(after.keys() - before.keys()))
    declared, skeletons, problems = _declared_removals(record)
    unanswered = tuple(
        item for item in removed if not any(_answers(line, item) for line in declared)
    )
    unused = tuple(line for line in declared if not any(_answers(line, item) for item in removed))
    named = [
        skeleton
        for skeleton in skeletons
        if any(_answers(Removal(skeleton.declared, "", ""), item) for item in removed)
    ]
    return RemovalReport(
        removed,
        added,
        declared,
        unanswered,
        unused,
        problems
        + tuple(
            f"a `Policy-Removal` line names {skeleton.declared!r} but leaves a field unanswered: "
            f"both fields after the name are required, and neither may be a placeholder: "
            f"{skeleton.line}"
            for skeleton in named
        ),
    )
