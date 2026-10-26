from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import TypeGuard, assert_never

from meizel.graph.model import AmbientKind
from meizel.graph.universe import GitRepo

__all__ = [
    "POLICY",
    "RETIRED_AMBIENT_SECTIONS",
    "RULES",
    "Policy",
    "invalidate_cache",
    "load",
    "load_snapshot",
    "parse",
    "parse_snapshot",
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
