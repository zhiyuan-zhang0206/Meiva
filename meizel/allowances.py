from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import cast

__all__ = ["POLICY", "RULES", "Policy", "load", "parse"]

POLICY = "meizel.policy.toml"
CHECKER_TABLES = ("external", "loads", "default-arguments")

REASONS = "reasons"  # a table of `name = "reason"`
NAMES = "names"  # a list of names

# rule -> section -> shape. A rule listed in `PER_NAME` has one reasons table per name it governs.
RULES: dict[str, dict[str, str]] = {
    "ambient-state": {
        "sink-callees": NAMES,
        "sink-callee-suffixes": NAMES,
        "wiring-callees": NAMES,
        "pure-callees": NAMES,
        "pure-chain-callees": NAMES,
        "skill-script-patterns": NAMES,
        "config-modules": NAMES,
        "config-readers": NAMES,
        "db-modules": NAMES,
        "db-primitives": NAMES,
        "db-handles": NAMES,
        "bus-handles": NAMES,
        "endpoint-handles": NAMES,
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
    },
}
PER_NAME = frozenset({"single-owner"})


@dataclass(slots=True)
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


def _reasons(table: object, where: str, problems: list[str]) -> dict[str, str]:
    if not isinstance(table, dict):
        problems.append(f"{where} must be a table of `name = reason`")
        return {}
    found: dict[str, str] = {}
    for name, reason in cast("dict[str, object]", table).items():
        if isinstance(reason, str) and reason.strip():
            found[name] = reason.strip()
        else:
            problems.append(f"{where} {name!r} needs a non-empty reason string")
    return found


def _names(value: object, where: str, problems: list[str]) -> tuple[str, ...]:
    items = cast("list[object]", value) if isinstance(value, list) else None
    if items is None or not all(isinstance(item, str) and item for item in items):
        problems.append(f"{where} must be a list of non-empty strings")
        return ()
    return tuple(cast("list[str]", items))


def _rule(rule: str, table: object, policy: Policy, problems: list[str]) -> None:
    if not isinstance(table, dict):
        problems.append(f"[{rule}] must be a table")
        return
    for section, value in cast("dict[str, object]", table).items():
        shape = RULES[rule].get(section, REASONS) if rule in PER_NAME else RULES[rule].get(section)
        where = f"[{rule}] {section}" if shape == NAMES else f"[{rule}.{section}]"
        if shape == REASONS:
            policy.listed[(rule, section)] = _reasons(value, where, problems)
        elif shape == NAMES:
            if (
                rule == "purity"
                and section == "max-uses"
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 1
            ):
                policy.named[(rule, section)] = (str(value),)
            else:
                policy.named[(rule, section)] = _names(value, where, problems)
        else:
            known = ", ".join(sorted(RULES[rule]))
            problems.append(f"[{rule}] has no section {section!r}; the sections are {known}")


def _checker(document: dict[str, object], policy: Policy, problems: list[str]) -> None:
    external = document.get("external", {})
    loads = document.get("loads", {})
    defaults = document.get("default-arguments", {})
    if not (isinstance(external, dict) and isinstance(loads, dict) and isinstance(defaults, dict)):
        problems.append("every table must be a table")
        return
    policy.external = _reasons(cast("dict[str, object]", external), "[external]", problems)
    for path, targets in cast("dict[str, object]", loads).items():
        if isinstance(targets, list) and all(
            isinstance(t, str) for t in cast("list[object]", targets)
        ):
            policy.loads[path] = tuple(cast("list[str]", targets))
        else:
            problems.append(f"[loads] {path!r} must be a list of directories")
    policy.default_arguments = tuple(cast("dict[str, object]", defaults))


def parse(text: str) -> tuple[Policy, list[str]]:
    """The policy in `text` and every defect of its shape; valid entries are kept."""
    policy = Policy(lines=text.splitlines())
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        return policy, [f"invalid TOML: {error}"]
    problems: list[str] = []
    for name in document:
        if name not in CHECKER_TABLES and name not in RULES:
            tables = ", ".join((*CHECKER_TABLES, *RULES))
            problems.append(f"unknown table {name!r}; the tables are {tables}")
    _checker(document, policy, problems)
    for rule in RULES:
        if rule in document:
            _rule(rule, document[rule], policy, problems)
    return policy, problems


@cache
def load(root: Path) -> Policy:
    """The policy of the checkout at `root`; an absent file lists nothing. A defect is an error."""
    path = root / POLICY
    if not path.is_file():
        return Policy()
    policy, problems = parse(path.read_text(encoding="utf-8"))
    if problems:
        raise ValueError(f"{POLICY}: {problems[0]}")
    return policy
