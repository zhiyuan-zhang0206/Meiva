# meiva:note
# A report row is a measured budget, not a diagnostic. Owners supply their actual limits;
# the shared ten-percent attention rule never creates a new gate or truncates the full table.
from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

from meizel.source_text import object_of

__all__ = ["BudgetRow", "changed_location", "decode_rows", "render_table", "source_rows"]


@dataclass(frozen=True, slots=True)
class BudgetRow:
    kind: str
    location: str
    current: int
    limit: int
    changed: bool

    @property
    def remaining(self) -> int:
        return self.limit - self.current

    @property
    def attention(self) -> bool:
        return self.changed and self.remaining * 10 <= self.limit


def changed_location(location: str, changed: Collection[str]) -> bool:
    return any(path == location or path.startswith(f"{location}/") for path in changed)


def _raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _mapping(value: object) -> TypeGuard[dict[str, object]]:
    return _raw_dict(value) and all(isinstance(key, str) for key in value)


def _list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


# meiva:note
# Supplemental snapshots cross a process boundary. Reject the entire malformed snapshot;
# a missing reading is never presented as a known zero or a partially complete pool report.
def decode_rows(text: str) -> list[BudgetRow]:
    value = object_of(json.loads, text)
    if not _mapping(value) or value.get("schema") != "meizel.budgets/v1":
        raise ValueError("expected meizel.budgets/v1 snapshot")
    rows = value.get("rows")
    if not _list(rows):
        raise ValueError("budget rows must be an array")
    return [_decode_row(row) for row in rows]


def _decode_row(row: object) -> BudgetRow:
    if not _mapping(row):
        raise ValueError("budget row must be an object")
    kind, location = row.get("kind"), row.get("location")
    current, limit, changed = row.get("current"), row.get("limit"), row.get("changed")
    if not isinstance(kind, str) or not kind or not isinstance(location, str) or not location:
        raise ValueError("budget kind and location must be nonempty strings")
    if type(current) is not int or current < 0 or type(limit) is not int or limit <= 0:
        raise ValueError("budget current must be nonnegative and limit positive integers")
    if not isinstance(changed, bool):
        raise TypeError("budget changed must be a boolean")
    return BudgetRow(kind, location, current, limit, changed)


def _cell(text: str) -> str:
    return text.replace("\n", "\\n").replace("\r", "\\r").replace("|", "\\|")


def render_table(rows: Iterable[BudgetRow]) -> str:
    lines = [
        "| Kind | Location | Current | Limit | Remaining | Changed |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for row in sorted(rows, key=lambda row: (row.kind, row.location)):
        marker = "**yes**" if row.attention else ("yes" if row.changed else "no")
        lines.append(
            f"| {_cell(row.kind)} | {_cell(row.location)} | {row.current} | "
            f"{row.limit} | {row.remaining} | {marker} |"
        )
    return "\n".join(lines) + "\n"


# meiva:note
# Source caps are supplied by their owner. These values use the existing splitlines arithmetic
# and repository-relative labels for both the hard gate and the advisory table.
def source_rows(
    texts: Mapping[Path, str],
    root: Path,
    directories: Mapping[str, int],
    changed: Collection[str],
    file_limit: int,
    directory_limit: int,
) -> list[BudgetRow]:
    rows: list[BudgetRow] = []
    for path, text in texts.items():
        name = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()
        rows.append(
            BudgetRow("python-lines", name, len(text.splitlines()), file_limit, name in changed)
        )
    rows.extend(
        BudgetRow(
            "directory-entries", name, count, directory_limit, changed_location(name, changed)
        )
        for name, count in directories.items()
    )
    return rows
