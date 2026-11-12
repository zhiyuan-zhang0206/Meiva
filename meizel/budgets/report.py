# meiva:note
# A report row is a measured budget, not a diagnostic. Owners supply their actual limits;
# the shared ten-percent attention rule never creates a new gate or truncates the full table.
from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

__all__ = ["BudgetRow", "changed_location", "render_table", "source_rows"]


@dataclass(frozen=True, slots=True)
class BudgetRow:
    kind: str
    location: str
    current: int
    limit: int
    changed: bool
    source: str = ""

    @property
    def remaining(self) -> int:
        return self.limit - self.current

    @property
    def attention(self) -> bool:
        return self.changed and self.remaining * 10 <= self.limit


def changed_location(location: str, changed: Collection[str]) -> bool:
    return any(path == location or path.startswith(f"{location}/") for path in changed)


def _cell(text: str) -> str:
    return text.replace("\n", "\\n").replace("\r", "\\r").replace("|", "\\|")


def render_table(rows: Iterable[BudgetRow]) -> str:
    rows = list(rows)
    with_source = any(row.source for row in rows)
    lines = [
        "| Kind | Location | Current | Limit | Remaining | Changed |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    if with_source:
        lines = [lines[0] + " Source |", lines[1] + " --- |"]
    for row in sorted(rows, key=lambda row: (row.kind, row.location, row.source)):
        marker = "**yes**" if row.attention else ("yes" if row.changed else "no")
        lines.append(
            f"| {_cell(row.kind)} | {_cell(row.location)} | {row.current} | "
            f"{row.limit} | {row.remaining} | {marker} |"
            + (f" {_cell(row.source)} |" if with_source else "")
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
