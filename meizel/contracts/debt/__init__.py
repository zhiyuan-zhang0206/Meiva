# meiva:note
# Recorded locality debt: the baseline format, its keys and the `[debt]` text of a manifest.
#
# A baseline lives in the `meizel.toml` of the directory that judges the violation, so debt is as
# local as the rule: an edge violation sits in the lowest common directory of the edge, a restricted
# form in the directory of its file. Entries carry no line numbers, so code that moves inside a file
# leaves the baseline alone.
#
# ```toml
# [debt.private-name]
# "test_a.py -> a.py#_helper" = 3
#
# [debt.cycle-edge]
# "b.py -> a.py" = 1
# ```

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from meizel.contracts.debt.entry import (
    DebtEntry,
    count_of,
    entry_value,
    is_table,
    read_entry,
    unowned,
)
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
    "DebtEntry",
    "DebtTable",
    "count_of",
    "entry_value",
    "file_key",
    "is_table",
    "local_edge",
    "parse_debt",
    "read_entry",
    "set_debt",
    "source_of",
    "total",
    "unowned",
]

CYCLE_EDGE = Code.CYCLE_EDGE
EDGE_CODES = frozenset(
    {Code.UNDECLARED_EDGE, Code.NOT_EXPORTED, Code.PRIVATE_MODULE, Code.PRIVATE_NAME, CYCLE_EDGE}
)
# meiva:note
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


type DebtTable = dict[Code, dict[str, DebtEntry]]

_NAMED = frozenset({Code.PRIVATE_NAME})
_ENTRY_FIELDS = ("count", "owner", "until")
_SEPARATOR = " -> "
_DEBT_HEADER = re.compile(r"^\s*\[\s*debt\s*(?:\.[^\]]*)?\]\s*(?:#.*)?$")
_ANY_HEADER = re.compile(r"^\s*\[[^\]]*\]\s*(?:#.*)?$")
_DEBT_CODE = re.compile(r"^\s*\[\s*debt\s*\.\s*([^\]]*?)\s*\]")
_DEBT_ROW = re.compile(r'^\s*("(?:[^"\\]|\\.)*")\s*=')
_COMMENT = re.compile(r"^\s*#")


def _within(owner: str, path: str) -> str:
    return path[len(owner) + 1 :] if owner else path


# meiva:note
# `file#what`, relative to the owning directory.
def file_key(owner: str, path: str, what: str) -> str:
    return f"{_within(owner, path)}#{what}"


# meiva:note
# The repository path of the file a debt is about.
def source_of(owner: str, code: Code, key: str) -> str:
    head = key.split(_SEPARATOR, 1)[0] if code in EDGE_CODES else key.split("#", 1)[0]
    return f"{owner}/{head}" if owner else head


# meiva:note
# The `(from, to)` children of the owner that an edge key projects onto.
def local_edge(key: str) -> tuple[str, str]:
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


# meiva:note
# Validate a parsed `[debt]` table; every defect goes to `report`, valid entries are kept.
def parse_debt(table: object, report: Callable[[str], None]) -> DebtTable:
    if not is_table(table):
        report("[debt] must be a table")
        return {}
    result: DebtTable = {}
    for name, rows in table.items():
        code = next((c for c in DEBTABLE if c.value == name), None)
        if code is None:
            known = ", ".join(sorted(c.value for c in DEBTABLE))
            report(f"[debt.{name}] is not a code that can be recorded; the codes are {known}")
        elif not is_table(rows):
            report(f"[debt.{name}] must be a table of `key = count`")
        else:
            kept = _valid_entries(code, rows, report)
            if kept:
                result[code] = kept
    return result


def _valid_entries(
    code: Code, rows: Mapping[str, object], report: Callable[[str], None]
) -> dict[str, DebtEntry]:
    kept: dict[str, DebtEntry] = {}
    for key, value in rows.items():
        where = f"[debt.{code}] {key!r}"
        problem = _key_problem(code, key)
        if problem is not None:
            report(f"{where}: {problem}")
            continue
        entry = read_entry(value, where, report)
        if entry is not None:
            kept[key] = entry
    return kept


def total(table: DebtTable) -> int:
    return sum(entry.count for rows in table.values() for entry in rows.values())


# meiva:note
# The `[debt.<code>]` tables for `table`, sorted, one debt per line; empty when none.
def _render_debt(table: DebtTable) -> str:
    blocks = [
        f"[debt.{code}]\n"
        + "\n".join(
            f"{json.dumps(key, ensure_ascii=False)} = {_render_entry(entry)}"
            for key, entry in sorted(table[code].items())
        )
        for code in sorted(table, key=str)
        if table[code]
    ]
    return "\n\n".join(blocks) + "\n" if blocks else ""


def _render_entry(entry: DebtEntry) -> str:
    value = entry_value(entry)
    if isinstance(value, int):
        return str(value)
    fields = ", ".join(
        f"{key} = {json.dumps(item, ensure_ascii=False) if isinstance(item, str) else item}"
        for key, item in value.items()
    )
    return "{ " + fields + " }"


# meiva:note
# Where a manifest keeps its debt tables: from the first `[debt.*]` header to the next section, or
# the end of the file. `(-1, -1)` when the manifest records none.
def _debt_span(lines: list[str]) -> tuple[int, int]:
    start = next((i for i, line in enumerate(lines) if _DEBT_HEADER.match(line)), -1)
    if start < 0:
        return -1, -1
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if _ANY_HEADER.match(lines[index]) and not _DEBT_HEADER.match(lines[index])
        ),
        len(lines),
    )
    return start, end


# meiva:note
# The entries `table` records under `name`, sorted, or None when it records none: a code the manifest
# has and the table does not is a table the rewrite drops.
def _entries_named(table: DebtTable, name: str) -> list[tuple[str, DebtEntry]] | None:
    for code, rows in table.items():
        if str(code) == name:
            return sorted(rows.items()) if rows else None
    return None


# meiva:note
# `lines` without the blank lines at either end: a blank line separates two blocks of the section, so
# it belongs to neither.
def _trim(lines: list[str]) -> list[str]:
    first = next((index for index, line in enumerate(lines) if line.strip()), len(lines))
    last = next((index for index in range(len(lines) - 1, -1, -1) if lines[index].strip()), -1)
    return lines[first : last + 1]


def _row(key: str, entry: DebtEntry) -> str:
    return f"{json.dumps(key, ensure_ascii=False)} = {_render_entry(entry)}"


# meiva:note
# A manifest's debt section as it was written: the tables in the order it had them, the comment above
# each one, each entry's own comment, and the comments that follow the last table.
@dataclass(frozen=True, slots=True)
class _Written:
    names: list[str]
    above: dict[str, list[str]]
    rows: dict[str, dict[str, list[str]]]
    tail: list[str]


def _read_section(section: list[str]) -> _Written:
    names: list[str] = []
    above: dict[str, list[str]] = {}
    rows: dict[str, dict[str, list[str]]] = {}
    pending: list[str] = []
    name = ""
    for line in section:
        header = _DEBT_CODE.match(line)
        row = _DEBT_ROW.match(line)
        if header is not None:
            name = header.group(1)
            if name not in names:
                names.append(name)
            above[name] = _trim(pending)
            pending = []
        elif row is not None and name:
            rows.setdefault(name, {})[json.loads(row.group(1))] = _trim(pending)
            pending = []
        elif _COMMENT.match(line) or not line.strip():
            pending.append(line.rstrip())
    return _Written(names, above, rows, _trim(pending))


# meiva:note
# One table's lines: its comment, its header, each entry under the comment it had, and the comments of
# the entries the rewrite drops after the ones that remain. `prune` deletes an entry, not the reason
# somebody wrote it down, so a dropped entry leaves its comment behind.
def _table_block(
    name: str, entries: list[tuple[str, DebtEntry]], written: dict[str, list[str]], lead: list[str]
) -> list[str]:
    kept = [*lead, f"[debt.{name}]"]
    for key, entry in entries:
        kept.extend(written.get(key, []))
        kept.append(_row(key, entry))
    surviving = {key for key, _ in entries}
    dropped = [line for key in written if key not in surviving for line in written[key]]
    return [*kept, "", *dropped] if dropped else kept


# meiva:note
# The debt section rewritten to `table`. A comment above a table or above one entry stays above it; a
# table the rewrite drops leaves its comments where the table was; and the comments after the last
# table are the section's tail, so a note above the next section stays above it --
# `base/telemetry/meizel.toml` states why its one root reads the environment in exactly that place.
# Entries come out sorted, as `_render_debt` writes them; blank lines inside the section are re-emitted
# one between blocks, and the blank line that separates the section from its neighbours belongs to the
# caller.
def _rewrite_section(section: list[str], table: DebtTable) -> list[str]:
    written = _read_section(section)
    blocks: list[str] = []
    for name in written.names:
        entries = _entries_named(table, name)
        rows = written.rows.get(name, {})
        block = (
            [*written.above[name], *(line for key in rows for line in rows[key])]
            if entries is None
            else _table_block(name, entries, rows, written.above[name])
        )
        if block:
            blocks.append("\n".join(block))
    for added in sorted({str(code) for code in table} - set(written.names)):
        entries = _entries_named(table, added)
        if entries:
            blocks.append("\n".join(_table_block(added, entries, {}, [])))
    if written.tail:
        blocks.append("\n".join(written.tail))
    return "\n\n".join(blocks).splitlines() if blocks else []


# meiva:note
# `text` with its `[debt.*]` tables replaced by `table`; everything else is kept as written, and the
# tables stay where the manifest had them. This used to rewrite them at the end of the file, which
# moved a `[roots]` table above its own debt and dropped the notes that sat in between.
def set_debt(text: str, table: DebtTable) -> str:
    lines = text.splitlines()
    start, end = _debt_span(lines)
    if start < 0:
        debt = _render_debt(table)
        body = text.rstrip("\n")
        if not debt:
            return f"{body}\n" if body else ""
        return f"{body}\n\n{debt}" if body else debt
    section = _rewrite_section(lines[start:end], table)
    tail = [""] if section and end < len(lines) and not lines[end - 1].strip() else []
    merged = [*lines[:start], *section, *tail, *lines[end:]]
    while merged and not merged[-1].strip():
        merged.pop()
    return "\n".join(merged) + "\n" if merged else ""


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
