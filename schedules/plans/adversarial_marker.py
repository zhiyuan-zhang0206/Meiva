# meiva:note
# The batch marker and the index lines a restart or a failed batch leaves behind.
#
# The marker lists the agents the running batch has spawned and is replaced atomically, so a
# restart reads either the previous list or the new one, never a half-written file. A marker
# found at the start of a batch means the previous process died mid-batch: that is recorded
# as an alerted index line, its workers are terminated and the owner is woken. The index is
# append-only JSON lines under `results/index.jsonl`.
# meiva:link schedules/docs/adversarial-eval.meiva.okf.md

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TypeGuard

from schedules.plans.adversarial_cases import batch_week

__all__ = [
    "BatchMarker",
    "StaleBatch",
    "error_line",
    "read_marker",
    "recovery_line",
    "render_marker",
    "with_colleague",
    "with_probe",
]

_TRACEBACK_TAIL = 4000


@dataclass(frozen=True)
class BatchMarker:
    week: str
    utc_timestamp: str
    probe_ids: tuple[int, ...]
    colleague_ids: tuple[int, ...]


# meiva:note
# What a marker left behind says: the week it recorded (`None` when the file was unreadable) and
# every worker id it names.
@dataclass(frozen=True)
class StaleBatch:
    week: str | None
    agent_ids: frozenset[int]


def with_probe(marker: BatchMarker, probe_id: int) -> BatchMarker:
    return replace(marker, probe_ids=(*marker.probe_ids, probe_id))


def with_colleague(marker: BatchMarker, colleague_id: int) -> BatchMarker:
    return replace(marker, colleague_ids=(*marker.colleague_ids, colleague_id))


def render_marker(marker: BatchMarker) -> str:
    payload = {
        "week": marker.week,
        "utc_timestamp": marker.utc_timestamp,
        "probe_ids": list(marker.probe_ids),
        "colleague_ids": list(marker.colleague_ids),
    }
    return json.dumps(payload, ensure_ascii=False) + "\n"


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_str_dict(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(key, str) for key in value)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _worker_ids(ids: object) -> set[int]:
    if not _is_object_list(ids):
        return set()
    return {
        agent_id for agent_id in ids if isinstance(agent_id, int) and not isinstance(agent_id, bool)
    }


# meiva:note
# A marker that is not valid JSON, or not an object, names no week and no workers: the batch it
# belongs to is still recorded as aborted, with the current week as a fallback.
def read_marker(text: str) -> StaleBatch:
    try:
        loaded: object = json.loads(text)
    except json.JSONDecodeError:
        return StaleBatch(None, frozenset())
    if not _is_str_dict(loaded):
        return StaleBatch(None, frozenset())
    week = loaded.get("week")
    ids = _worker_ids(loaded.get("probe_ids")) | _worker_ids(loaded.get("colleague_ids"))
    return StaleBatch(week if isinstance(week, str) else None, frozenset(ids))


def recovery_line(stale: StaleBatch, fallback_week: str, now: datetime) -> str:
    event = {
        "week": stale.week if stale.week is not None else fallback_week,
        "utc_timestamp": now.isoformat(),
        "error": "aborted by process restart",
        "alerted": True,
    }
    return json.dumps(event, ensure_ascii=False) + "\n"


# meiva:note
# A batch that raised: the week of `now` and the last 4000 characters of the traceback.
def error_line(now: datetime, detail: str) -> str:
    event = {
        "week": batch_week(now).label,
        "utc_timestamp": now.isoformat(),
        "error": detail[-_TRACEBACK_TAIL:],
        "alerted": True,
    }
    return json.dumps(event, ensure_ascii=False) + "\n"
