# meiva:note
# What the schema drift audit decides: the declared columns, the live difference, its class.
#
# `schema_drift.py` reads `db/schema.sql`, queries the live database and writes the report; parsing
# the DDL, comparing it with the live columns and classifying what is left over are here.

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

__all__ = [
    "Drift",
    "allowlist_entries",
    "classify_column",
    "classify_table",
    "compare",
    "live_columns",
]

_PARTITION = re.compile(r"^(?P<parent>.+)_(?P<suffix>\d{6}|default)$")
_RUNTIME_DDL = ("checkpoint",)


@dataclass(frozen=True)
class Drift:
    extra_tables: list[str] = field(default_factory=list[str])
    missing_tables: list[str] = field(default_factory=list[str])
    extra_columns: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])
    missing_columns: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def render(self, declared: set[str]) -> str:
        # meiva:note
        # The report as text, one line per difference.
        lines = ["# schema drift", ""]
        for name in self.extra_tables:
            lines.append(f"extra table   {name}  [{classify_table(name, declared)}]")
        for table, column, meta in self.extra_columns:
            lines.append(f"extra column  {table}.{column}  [{classify_column(meta)}]")
        for name in self.missing_tables:
            lines.append(f"missing table {name}")
        for table, column in self.missing_columns:
            lines.append(f"missing column {table}.{column}")
        if len(lines) == 2:
            lines.append("clean")
        return "\n".join(lines)

    def to_json(self, declared: set[str]) -> dict[str, object]:
        # meiva:note
        # The drift as the structure the `--json` report writes.
        return {
            "extra_tables": [
                {"name": name, "class": classify_table(name, declared)}
                for name in self.extra_tables
            ],
            "extra_columns": [
                {"table": table, "column": column, "class": classify_column(meta)}
                for table, column, meta in self.extra_columns
            ],
            "missing_tables": self.missing_tables,
            "missing_columns": self.missing_columns,
        }

    def allowed_only(self, allowed: frozenset[str]) -> Drift:
        # meiva:note
        # The drift without the entries an allowlist names (`table` or `table.column`).
        return Drift(
            extra_tables=[name for name in self.extra_tables if name not in allowed],
            missing_tables=self.missing_tables,
            extra_columns=[
                column for column in self.extra_columns if f"{column[0]}.{column[1]}" not in allowed
            ],
            missing_columns=self.missing_columns,
        )

    def is_dirty(self) -> bool:
        # meiva:note
        # Whether any difference is left.
        return bool(
            self.extra_tables or self.extra_columns or self.missing_tables or self.missing_columns
        )


def compare(schema: dict[str, list[str]], live: dict[str, dict[str, str]]) -> Drift:
    # meiva:note
    # The difference between the declared DDL and the live columns, case-insensitively.
    schema_lower = {name.lower(): cols for name, cols in schema.items()}
    live_lower = {name.lower(): cols for name, cols in live.items()}
    extra_tables = sorted(set(live_lower) - set(schema_lower))
    missing_tables = sorted(set(schema_lower) - set(live_lower))
    extra_columns: list[tuple[str, str, str]] = []
    missing_columns: list[tuple[str, str]] = []
    for table in sorted(set(schema_lower) & set(live_lower)):
        declared = {column.lower() for column in schema_lower[table]}
        present = {column.lower() for column in live_lower[table]}
        for column in sorted(present - declared):
            extra_columns.append((table, column, live_lower[table][column]))
        for column in sorted(declared - present):
            missing_columns.append((table, column))
    return Drift(
        extra_tables=extra_tables,
        missing_tables=missing_tables,
        extra_columns=extra_columns,
        missing_columns=missing_columns,
    )


# meiva:link scripts/audit/docs/schema-drift.meiva.okf.md
def classify_table(name: str, declared: set[str]) -> str:
    # meiva:note
    # Why a live table is not in the DDL: a runtime partition, runtime DDL, or history.
    match = _PARTITION.match(name)
    if match and match.group("parent") in declared:
        return "design/runtime-partition"
    if name.startswith(_RUNTIME_DDL):
        return "design/runtime-ddl"
    return "historical"


def classify_column(meta: str) -> str:
    # meiva:note
    # Why a live column is not in the DDL: required without a default is dangerous.
    nullable, _, default = meta.partition("|")
    if nullable == "NO" and not default:
        return "dangerous"
    return "historical"


# meiva:note
# The live columns as the audit compares them: `table -> column -> "nullable|default"`. The query
# and its rows are the shell's; the metadata encoding is the one `classify_column` decodes.
def live_columns(rows: Sequence[tuple[str, str, str, str | None]]) -> dict[str, dict[str, str]]:
    live: dict[str, dict[str, str]] = {}
    for table, column, nullable, default in rows:
        live.setdefault(table, {})[column] = f"{nullable}|{default or ''}"
    return live


# meiva:note
# The entries an allowlist file names: one `table` or `table.column` per line, blank lines and
# surrounding spaces ignored.
def allowlist_entries(text: str) -> frozenset[str]:
    return frozenset(line.strip() for line in text.splitlines() if line.strip())
