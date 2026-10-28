"""Recorded locality debt: the baseline format, its keys and the `[debt]` text of a manifest.

A baseline lives in the `meizel.toml` of the directory that judges the violation, so debt is as
local as the rule: an edge violation sits in the lowest common directory of the edge, a
restricted form in the directory of its file. Entries carry no line numbers, so code that moves
inside a file leaves the baseline alone.

```toml
[debt.private-name]
"test_a.py -> a.py#_helper" = 3

[debt.cycle-edge]
"b.py -> a.py" = 1
```
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import TypeGuard

from meizel.graph.model import Code

__all__ = [
    "AMBIENT_CODES",
    "CYCLE_EDGE",
    "DEBTABLE",
    "EDGE_CODES",
    "ENTROPY_CODES",
    "PATCH_CODES",
    "PATCH_HINT",
    "PATCH_REASONING",
    "PATCH_WHAT",
    "PURITY_CODES",
    "TABLE_KEY",
    "DebtTable",
    "file_key",
    "local_edge",
    "parse_debt",
    "set_debt",
    "source_of",
    "total",
]

CYCLE_EDGE = Code.CYCLE_EDGE
EDGE_CODES = frozenset(
    {Code.UNDECLARED_EDGE, Code.NOT_EXPORTED, Code.PRIVATE_MODULE, Code.PRIVATE_NAME, CYCLE_EDGE}
)
# The codes of a patch: the key of an entry is `file#form target`.
PATCH_CODES = (
    Code.FIRST_PARTY_PATCH,
    Code.STDLIB_PATCH,
    Code.THIRD_PARTY_PATCH,
    Code.ENV_PATCH,
    Code.UNRESOLVED_PATCH,
    Code.PATCH_RAISING_FALSE,
    Code.TEST_SUBCLASS_OVERRIDE,
)
PURITY_CODES = (
    Code.IMPURE_EDGE,
    Code.IMPURE_CALL,
    Code.UNFROZEN_VALUE,
    Code.UNTYPED_EXPORT,
    Code.IMPURE_TEST_FIXTURE,
    Code.STALE_EXPORT,
    Code.PASSTHROUGH_FUNCTION,
    Code.NESTED_ATTR_WRITE,
    Code.NESTED_PATCH_TARGET,
    Code.DOMAIN_SUBSET_LEAK,
    Code.WIDE_FANOUT,
    Code.WIDE_EXPORTS,
    Code.WIDE_PUBLIC_SURFACE,
    Code.WIDE_PARAMS,
    Code.DEEP_ATTR_CHAIN,
)
# meiva:note
# The code of an ambient read: the key of an entry is `file#kind target`.
AMBIENT_CODES = (Code.AMBIENT_READ,)
# meiva:note
# The code of a spent reading: the key of an entry is `file#reading in where`.
ENTROPY_CODES = (Code.FAKE_ENTROPY,)
_FILE_CODES = frozenset(
    {
        Code.RESTRICTED,
        Code.TYPE_CAST,
        Code.ANY_COLLABORATOR,
        *PATCH_CODES,
        *PURITY_CODES,
        *AMBIENT_CODES,
        *ENTROPY_CODES,
    }
)
DEBTABLE = EDGE_CODES | _FILE_CODES
TABLE_KEY = "debt"

type DebtTable = dict[Code, dict[str, int]]

_NAMED = frozenset({Code.PRIVATE_NAME})
_SEPARATOR = " -> "
_DEBT_HEADER = re.compile(r"^\s*\[\s*debt\s*(?:\.[^\]]*)?\]\s*(?:#.*)?$")
_ANY_HEADER = re.compile(r"^\s*\[[^\]]*\]\s*(?:#.*)?$")


def _within(owner: str, path: str) -> str:
    return path[len(owner) + 1 :] if owner else path


def file_key(owner: str, path: str, what: str) -> str:
    """`file#what`, relative to the owning directory."""
    return f"{_within(owner, path)}#{what}"


def source_of(owner: str, code: Code, key: str) -> str:
    """The repository path of the file a debt is about."""
    head = key.split(_SEPARATOR, 1)[0] if code in EDGE_CODES else key.split("#", 1)[0]
    return f"{owner}/{head}" if owner else head


def local_edge(key: str) -> tuple[str, str]:
    """The `(from, to)` children of the owner that an edge key projects onto."""
    src, _, dst = key.partition("#")[0].partition(_SEPARATOR)
    return src.split("/", 1)[0], dst.split("/", 1)[0]


def _edge_problem(code: Code, key: str) -> str | None:
    head, _, name = key.partition("#")
    src, _, dst = head.partition(_SEPARATOR)
    if not src or not dst:
        return "an edge debt is written `source -> target`"
    if code in _NAMED and not name:
        return "a private-name debt is written `source -> target#name`"
    if code not in _NAMED and name:
        return f"a {code} debt is written `source -> target` with no name"
    return None


def _key_problem(code: Code, key: str) -> str | None:
    if code in EDGE_CODES:
        return _edge_problem(code, key)
    file, _, what = key.partition("#")
    return None if file and what else f"a {code} debt is written `file#form`"


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_table(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(k, str) for k in value)


def parse_debt(table: object, report: Callable[[str], None]) -> DebtTable:
    """Validate a parsed `[debt]` table; every defect goes to `report`, valid entries are kept."""
    if not _is_table(table):
        report("[debt] must be a table")
        return {}
    result: DebtTable = {}
    for name, rows in table.items():
        code = next((c for c in DEBTABLE if c.value == name), None)
        if code is None:
            known = ", ".join(sorted(c.value for c in DEBTABLE))
            report(f"[debt.{name}] is not a code that can be recorded; the codes are {known}")
        elif not _is_table(rows):
            report(f"[debt.{name}] must be a table of `key = count`")
        else:
            kept = _valid_entries(code, rows, report)
            if kept:
                result[code] = kept
    return result


def _valid_entries(
    code: Code, rows: Mapping[str, object], report: Callable[[str], None]
) -> dict[str, int]:
    kept: dict[str, int] = {}
    for key, count in rows.items():
        problem = _key_problem(code, key)
        if problem is not None:
            report(f"[debt.{code}] {key!r}: {problem}")
        elif not isinstance(count, int) or isinstance(count, bool) or count < 1:
            report(f"[debt.{code}] {key!r}: the count must be a positive integer")
        else:
            kept[key] = count
    return kept


def total(table: DebtTable) -> int:
    return sum(count for rows in table.values() for count in rows.values())


# meiva:note
# The `[debt.<code>]` tables for `table`, sorted, one debt per line; empty when none.
def _render_debt(table: DebtTable) -> str:
    blocks = [
        f"[debt.{code}]\n"
        + "\n".join(
            f"{json.dumps(key, ensure_ascii=False)} = {count}"
            for key, count in sorted(table[code].items())
        )
        for code in sorted(table, key=str)
        if table[code]
    ]
    return "\n\n".join(blocks) + "\n" if blocks else ""


def set_debt(text: str, table: DebtTable) -> str:
    """`text` with its `[debt.*]` tables replaced by `table`; everything else is kept as written."""
    kept: list[str] = []
    inside = False
    for line in text.splitlines():
        if _DEBT_HEADER.match(line):
            inside = True
        elif _ANY_HEADER.match(line):
            inside = False
        if not inside:
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    base = "\n".join(kept)
    debt = _render_debt(table)
    if not debt:
        return base + "\n" if base else ""
    return f"{base}\n\n{debt}" if base else debt


PATCH_WHAT = {
    Code.FIRST_PARTY_PATCH: "first-party",
    Code.STDLIB_PATCH: "standard-library",
    Code.THIRD_PARTY_PATCH: "third-party",
    Code.ENV_PATCH: "the environment",
    Code.UNRESOLVED_PATCH: "an unresolved target",
}
PATCH_HINT = {
    Code.FIRST_PARTY_PATCH: (
        "do not patch first-party code: split the code under test into a public facade and an "
        "implementation whose dependencies are explicit parameters, and let the test pass the "
        "fake in directly"
    ),
    Code.STDLIB_PATCH: (
        "do not patch the standard library: the code under test takes the dependency (a clock, a "
        "sleeper, a file opener) as an explicit parameter, and the test passes the fake in directly"
    ),
    Code.THIRD_PARTY_PATCH: (
        "do not patch a third-party package: wrap it behind a first-party seam whose dependency is "
        "an explicit parameter, and let the test pass the fake in directly"
    ),
    Code.ENV_PATCH: (
        "do not patch the environment: read the setting once at the boundary and pass the value "
        "in as an explicit parameter"
    ),
    Code.UNRESOLVED_PATCH: (
        "the target of this patch is not an imported module or object, so what it replaces is "
        "invisible to the checker: inject the dependency as an explicit parameter instead of "
        "patching it"
    ),
}
# meiva:note
# The two codes below carry their principle, recipe and rule as three lines; the target is zero,
# not a long-term debt.
PATCH_REASONING = {
    Code.PATCH_RAISING_FALSE: (
        "why: `raising=False` turns a patch of a name that no longer exists into a silent no-op, so "
        "the test keeps passing while it stops testing what it claims",
        "instead: inject the dependency as a parameter; a name that is optional by design is "
        "asserted to exist before it is patched, or the test builds the object that lacks it. "
        "`missing-patch-target` is the same defect on a first-party module and cannot be recorded",
        "see: docs/conventions/python-conventions.md#test-seams-in-another-shape",
    ),
    Code.TEST_SUBCLASS_OVERRIDE: (
        "why: a test class that overrides one method of production code replaces behavior by "
        "name, a patch in another spelling; later changes of the base silently stop being tested",
        "instead: give the production class the value it reads as a constructor field and build "
        "the real class with it; lift the overridden decision into a pure function; make the "
        "fault with a real resource; only a fault no real resource can make takes a narrow "
        "Protocol in base/ with its fake in base's tests/",
        "see: docs/conventions/python-conventions.md#test-seams-in-another-shape",
    ),
}
