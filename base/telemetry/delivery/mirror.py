"""The local JSONL event mirror: the durable fallback every ordinary event reaches first."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime, timedelta
from hashlib import blake2b
from pathlib import Path
from typing import Any

from base.telemetry.delivery.receipts import Event
from base.telemetry.delivery.reporting import failure_isolated, report_no_pipeline
from base.telemetry.delivery.serialization import event_line, event_payload
from base.telemetry.delivery.state import process_state

# JSONL mirror retention (day-stamped files, like the trace mirror).
JSONL_RETENTION_DAYS = 7


def event_id(line: str, ts_ns: int) -> int:
    """Return the stable surrogate id shared by mirror and Loki event rows."""
    return int.from_bytes(blake2b(f"{ts_ns}:{line}".encode(), digest_size=8).digest(), "big")


def event_row(event: Event) -> dict[str, Any]:
    """Canonical mirror row and stable identity shared by metric projection."""
    body = event_payload(event)
    body_str = event_line(event)
    ts_ns = int(event.ts.timestamp() * 1_000_000_000)
    return {**body, "id": event_id(body_str, ts_ns)}


# meiva:note
# Return whether a day-stamped mirror filename is older than ``cutoff`` (YYYYMMDD).
def is_expired_mirror_filename(filename: str, *, cutoff: str) -> bool:
    day = filename.removeprefix("events-").split(".", 1)[0]
    return len(day) == 8 and day.isdigit() and day < cutoff


def _utc_now() -> datetime:
    return datetime.now(UTC)


def prune_jsonl_mirror(
    *,
    target_dir: Path | None = None,
    retention_days: int = JSONL_RETENTION_DAYS,
) -> None:
    """Delete day-stamped mirror files older than their retention tier.

    Runs once per day (guarded by the `jsonl_day` stamp). The mirror is the
    durable fallback for audit events; log-stream lines are also held by the
    loguru file sinks, so the mirror's own retention is what bounds its disk
    footprint."""
    from base.paths import logs_dir

    resolved_dir = logs_dir() if target_dir is None else target_dir
    cutoff = (_utc_now() - timedelta(days=retention_days)).strftime("%Y%m%d")
    # meiva:note
    # Only plain `.jsonl` matches here, and that is deliberate: an older day may be
    # stored compressed as `events-YYYYMMDD.jsonl.zst` (recipe and recovery steps in
    # `~/.meiva/logs/README-events-compression.md`). A compressed day is out of THIS
    # retention — pruning cannot bound it — so it is removed by hand, not by age.
    #
    # `.rollup.jsonl` is the retired filtered tier; leftovers age out with the full mirror.
    for path in (
        *resolved_dir.glob("events-????????.jsonl"),
        *resolved_dir.glob("events-????????.rollup.jsonl"),
    ):
        if is_expired_mirror_filename(path.name, cutoff=cutoff):
            with contextlib.suppress(OSError):
                path.unlink()


def append_jsonl(events: list[Event], *, target_dir: Path | None = None) -> None:
    """Append the batch to today's mirror file, one JSON line per event.

    Each row carries the stable surrogate ``id`` derived from its id-free body
    and timestamp, matching the id Loki's read path returns for the same event.

    Best-effort — the mirror is a fallback, not a critical path; a write
    failure must never break the batch. But it must not be SILENT either:
    the mirror is the durable local copy, so a sustained mirror failure is
    reported (first + every 50th) — a disk-full / permission error would
    otherwise degrade it without a trace. Single write() per line with
    O_APPEND semantics (opened in append mode) keeps concurrent processes
    from interleaving."""
    from base.paths import logs_dir

    resolved_dir = logs_dir() if target_dir is None else target_dir
    day = _utc_now().strftime("%Y%m%d")
    try:
        lines = [
            json.dumps(event_row(e), default=str, separators=(",", ":"), ensure_ascii=False) + "\n"
            for e in events
        ]
        path = resolved_dir / f"events-{day}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write("".join(lines))
    except Exception as exc:  # report, never raise
        global jsonl_failures  # noqa: PLW0603 — module-level counter
        jsonl_failures += 1
        if jsonl_failures == 1 or jsonl_failures % 50 == 0:
            report_no_pipeline(
                "[event-emitter] JSONL mirror write failed ({n} consecutive) — "
                "the mirror is the durable local copy; a sustained failure "
                "means batches are not landing in it: {err}",
                n=jsonl_failures,
                err=repr(exc),
            )
    else:
        # Once per day, after a write that landed: a prune failure is its own report and
        # never a second one for the same disk fault.
        if process_state["jsonl_day"] != day:
            process_state["jsonl_day"] = day
            with failure_isolated("jsonl mirror prune"):
                prune_jsonl_mirror(target_dir=resolved_dir)


# ── drain-thread failure visibility ─────────────────────────────────────────
#
# The drain thread's mirror write failures used to be invisible (whole batch
# swallowed by suppress(), zero logs). This counter + the marker below keep a
# sustained failure loud without flooding: the first failure and every 50th
# after it are reported. (The PG write — the other former failure source — was
# retired with the LGTM cutover, task #1197.)
jsonl_failures = 0
