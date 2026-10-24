# meiva:note
# Which mirror files `meiva trace ship` replays, from where in each, and what it reports.
#
# Two modes: incremental (a per-file byte-offset watermark records how far each file was shipped)
# and windowed (`--since` / `--until` select whole files by the day stamp in their name, or the
# mtime for the unstamped active file, and ignore the watermark). Span ingestion is idempotent by
# span id, so re-shipping an already-shipped window is safe.

from __future__ import annotations

from base64 import b64encode
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime

__all__ = [
    "FileCursor",
    "ShipTotals",
    "ShipWindow",
    "file_report_line",
    "live_watermarks",
    "plan_file_cursor",
    "repaired_span_id",
    "ship_summary_lines",
    "ship_window",
]


# meiva:note
# The days a windowed ship selects, inclusive; `windowed` is false when neither bound was given.
# `scope` is how the report names the selection.
@dataclass(frozen=True)
class ShipWindow:
    since: date
    until: date
    windowed: bool
    scope: str

    # meiva:note
    # Whether a file stamped with `day` is inside the window.
    def contains(self, day: date) -> bool:
        return self.since <= day <= self.until


# meiva:note
# The window for `--since` / `--until` (`YYYY-MM-DD`, UTC days). A malformed day raises
# `ValueError`.
def ship_window(since: str | None, until: str | None) -> ShipWindow:
    low = datetime.strptime(since, "%Y-%m-%d").date() if since else date.min  # noqa: DTZ007
    high = datetime.strptime(until, "%Y-%m-%d").date() if until else date.max  # noqa: DTZ007
    windowed = since is not None or until is not None
    scope = f"window {since or '-inf'}..{until or '+inf'}" if windowed else "incremental"
    return ShipWindow(low, high, windowed=windowed, scope=scope)


# meiva:note
# Where to start in one mirror file: the watermark key and the byte offset, or no work.
#
# A gzipped segment (the agent-side compression pass renames a segment to `.jsonl.gz`) is keyed by
# its BASE name, so the pass never strands unshipped lines and a re-run after compression
# continues exactly where the plain file left off. Windowed mode starts every file at 0. A plain
# file whose watermark is at its end has nothing to ship; a gzip stream cannot tell its decoded
# size cheaply, so it is always opened and its shipped prefix is read and discarded.
@dataclass(frozen=True)
class FileCursor:
    key: str
    start: int
    gzipped: bool
    shipped_out: bool


def plan_file_cursor(
    name: str, *, windowed: bool, marks: Mapping[str, int], size: int
) -> FileCursor:
    gzipped = name.endswith(".gz")
    key = name.removesuffix(".gz")
    start = 0 if windowed else marks.get(key, 0)
    return FileCursor(key, start, gzipped, shipped_out=not gzipped and start >= size)


# meiva:note
# The watermark without entries for files the retention pass pruned out of the mirror.
#
# A key survives when its base file or the gzipped variant exists, so the watermark does not grow
# without bound as per-pid daily files cycle.
def live_watermarks(marks: Mapping[str, int], *, present: frozenset[str]) -> dict[str, int]:
    return {
        name: offset for name, offset in marks.items() if name in present or f"{name}.gz" in present
    }


# meiva:note
# The raw byte id that a stored `value` encodes, when it was written as base64 of hex.
#
# OTLP/JSON ids are hex strings, which protobuf's JSON parser reads as base64 and so decodes to
# the wrong length. Returns None when `value` already has the `expected_length` or cannot be
# repaired.
def repaired_span_id(value: bytes, expected_length: int) -> bytes | None:
    if not value or len(value) == expected_length:
        return None
    try:
        corrected = bytes.fromhex(b64encode(value).decode("ascii"))
    except ValueError:
        return None
    return corrected if len(corrected) == expected_length else None


# meiva:note
# The per-file report line.
def file_report_line(name: str, *, batches: int, spans: int) -> str:
    return f"  {name}: {batches} batches, {spans} spans"


# meiva:note
# What a ship sent: files with at least one line, batches (lines POSTed) and spans in them.
@dataclass(frozen=True)
class ShipTotals:
    files: int
    batches: int
    spans: int


# meiva:note
# The summary and target lines a ship ends with.
def ship_summary_lines(
    *, dry_run: bool, scope: str, totals: ShipTotals, target_line: str
) -> tuple[str, str]:
    verb = "would ship" if dry_run else "shipped"
    return (
        f"{verb} ({scope}): {totals.files} files, {totals.batches} batches, {totals.spans} spans",
        target_line,
    )
