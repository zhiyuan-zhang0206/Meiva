# meiva:note
# Snapshot ownership and coverage survive decoding and rendering. Declared completeness is
# not proof of live freshness: the external owner supplies provenance and measurement semantics.
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum

from meizel.budgets.report import BudgetRow, render_table
from meizel.source_text import is_mapping, is_object_list, object_of

SOURCE_KINDS = ("python-lines", "directory-entries")
POOL_KINDS = (
    "memory-markdown-files",
    "memory-subdirectories",
    "memory-note-characters",
    "memory-index-characters",
)
PROVENANCE_FIELDS = (
    "producer",
    "source_revision",
    "snapshot",
    "root",
    "inventory",
    "units",
    "effective_limits",
    "changed_root",
    "changed_snapshot",
)


class Coverage(StrEnum):
    NOT_PROVIDED = "not-provided"
    UNAVAILABLE = "unavailable"
    PARTIAL = "partial"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class Snapshot:
    owner: str
    rows: tuple[BudgetRow, ...]
    expected_kinds: tuple[str, ...] = ()
    provenance: Mapping[str, str] = field(default_factory=dict[str, str])
    unavailable: Mapping[str, str] = field(default_factory=dict[str, str])

    @property
    def missing_kinds(self) -> tuple[str, ...]:
        required = POOL_KINDS if self.owner == "memory-pool" else ()
        if self.owner == "source":
            required = SOURCE_KINDS
        expected = set(required).union(self.expected_kinds)
        return tuple(sorted(expected.difference(row.kind for row in self.rows)))

    @property
    def missing_provenance(self) -> tuple[str, ...]:
        return tuple(
            key
            for key in PROVENANCE_FIELDS
            if self.provenance.get(key, "").strip().lower()
            in {"", "unknown", "not-provided", "unavailable"}
        )

    @property
    def coverage(self) -> Coverage:
        if not self.rows and self.unavailable:
            return Coverage.UNAVAILABLE
        if (
            not self.rows
            or not self.expected_kinds
            or self.missing_kinds
            or self.missing_provenance
            or self.unavailable
        ):
            return Coverage.PARTIAL
        return Coverage.COMPLETE


def _strings(value: object, label: str) -> dict[str, str]:
    if not is_mapping(value):
        raise ValueError(f"{label} must be an object")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(item, str) or not item:
            raise ValueError(f"{label} values must be nonempty strings")
        result[key] = item
    return result


def _kinds(value: object) -> tuple[str, ...]:
    if not is_object_list(value):
        raise ValueError("expected_kinds must be an array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError("expected kinds must be nonempty strings")
        result.append(item)
    return tuple(result)


def _row(value: object) -> BudgetRow:
    if not is_mapping(value):
        raise ValueError("budget row must be an object")
    kind, location = value.get("kind"), value.get("location")
    current, limit, changed = value.get("current"), value.get("limit"), value.get("changed")
    if not isinstance(kind, str) or not kind or not isinstance(location, str) or not location:
        raise ValueError("budget kind and location must be nonempty strings")
    if type(current) is not int or current < 0 or type(limit) is not int or limit <= 0:
        raise ValueError("budget current must be nonnegative and limit positive integers")
    if not isinstance(changed, bool):
        raise TypeError("budget changed must be a boolean")
    return BudgetRow(kind, location, current, limit, changed)


def _binding(provenance: Mapping[str, str]) -> None:
    for original, claim in (("root", "changed_root"), ("snapshot", "changed_snapshot")):
        if (
            original in provenance
            and claim in provenance
            and provenance[original] != provenance[claim]
        ):
            raise ValueError(f"{claim} does not match {original}")


def decode_snapshot(text: str) -> Snapshot:
    value = object_of(json.loads, text)
    if not is_mapping(value) or value.get("schema") != "meizel.budgets/v1":
        raise ValueError("expected meizel.budgets/v1 snapshot")
    rows, owner = value.get("rows"), value.get("owner", "unattributed")
    if not is_object_list(rows) or not isinstance(owner, str) or not owner:
        raise ValueError("snapshot requires an owner string and rows array")
    provenance = _strings(value.get("provenance", {}), "provenance")
    _binding(provenance)
    return Snapshot(
        owner,
        tuple(_row(row) for row in rows),
        _kinds(value.get("expected_kinds", [])),
        provenance,
        _strings(value.get("unavailable", {}), "unavailable"),
    )


def _described(number: int, snapshot: Snapshot) -> str:
    lines = [f"Source {number}: owner={snapshot.owner!r}; coverage={snapshot.coverage.value}"]
    lines.extend(
        f"- {key}: {snapshot.provenance.get(key, 'not-provided')!r}"
        for key in dict.fromkeys((*PROVENANCE_FIELDS, *snapshot.provenance))
    )
    lines.append(f"- declared expected kinds: {snapshot.expected_kinds!r}")
    lines.append(f"- provided kinds: {sorted({row.kind for row in snapshot.rows})!r}")
    lines.append(f"- missing kinds: {', '.join(snapshot.missing_kinds) or 'none declared missing'}")
    lines.append(f"- unavailable: {dict(snapshot.unavailable)!r}")
    return "\n".join(lines) + "\n"


def render_snapshots(snapshots: Sequence[Snapshot], *, attention_only: bool = False) -> str:
    rows: list[BudgetRow] = []
    descriptions: list[str] = []
    for number, snapshot in enumerate(snapshots, 1):
        selected = [row for row in snapshot.rows if not attention_only or row.attention]
        if attention_only and not selected:
            continue
        descriptions.append(_described(number, snapshot))
        rows.extend(replace(row, source=f"{number}:{snapshot.owner}") for row in selected)
    pool = [s.coverage.value for s in snapshots if s.owner == "memory-pool"]
    coverage = ", ".join(pool) if pool else Coverage.NOT_PROVIDED.value
    header = f"Memory-pool coverage: {coverage} (report only).\n"
    return header + "\n".join(descriptions) + "\n" + render_table(rows)
