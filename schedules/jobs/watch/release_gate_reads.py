# meiva:note
# Scheduled history must not disappear behind manual/PR runs. Read the workflow's
# schedule-only stream, then a timestamp-bounded window anchored at its newest
# arrival. GitHub permits 100 rows/page and caps filtered searches at 1,000;
# missing, changing or capped pages cannot prove recovery or a complete count.
# REST contract: https://docs.github.com/en/rest/actions/workflow-runs
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import NamedTuple
from urllib.parse import quote, urlencode

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from schedules.plans.watch.release_gate_slot import RELEASE_GATE_SLOT_LOOKBACK

_PER_PAGE = 100
_SEARCH_LIMIT = 1000


class _Start(NamedTuple):
    identifier: int
    workflow_id: int
    at: datetime


def _page(path: str, read: Callable[[str], str]) -> tuple[int, tuple[object, ...]]:
    data = object_of(json.loads, read(path))
    if not is_object_dict(data):
        raise ValueError("scheduled run listing is not an object")
    total, rows = data.get("total_count"), data.get("workflow_runs")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ValueError("scheduled run listing has no valid total_count")
    if not is_object_list(rows):
        raise ValueError("scheduled run listing has no workflow_runs array")
    return total, tuple(rows)


def _start(raw: object, workflow: str, branch: str) -> _Start:
    if not is_object_dict(raw):
        raise ValueError("scheduled run row is not an object")
    identifier, workflow_id = raw.get("id"), raw.get("workflow_id")
    if any(isinstance(x, bool) or not isinstance(x, int) for x in (identifier, workflow_id)):
        raise ValueError("scheduled run row has no run/workflow identity")
    assert isinstance(identifier, int) and isinstance(workflow_id, int)
    path, stamp = raw.get("path"), raw.get("created_at")
    if (
        raw.get("event") != "schedule"
        or raw.get("head_branch") != branch
        or not isinstance(path, str)
        or path.partition("@")[0] != f".github/workflows/{workflow}"
        or not isinstance(stamp, str)
    ):
        raise ValueError("scheduled run row does not belong to the requested stream")
    point = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if point.tzinfo is None:
        raise ValueError("scheduled start has no timezone")
    return _Start(identifier, workflow_id, point)


def read_scheduled_starts(
    repo: str, workflow: str, *, branch: str, read: Callable[[str], str]
) -> tuple[datetime, ...]:
    endpoint = f"repos/{repo}/actions/workflows/{quote(workflow, safe='')}/runs"
    filters = {"event": "schedule", "branch": branch}
    total, latest_rows = _page(endpoint + "?" + urlencode({**filters, "per_page": 1}), read)
    if total < 1 or len(latest_rows) != 1:
        raise ValueError("no verifiable newest scheduled run")
    anchor = _start(latest_rows[0], workflow, branch)
    lower = anchor.at - RELEASE_GATE_SLOT_LOOKBACK
    bounds = "..".join(
        p.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        for p in (lower, anchor.at)
    )
    query = {**filters, "created": bounds, "per_page": _PER_PAGE}
    expected, rows = _page(endpoint + "?" + urlencode({**query, "page": 1}), read)
    if not 0 < expected < _SEARCH_LIMIT:
        raise ValueError("scheduled window is empty or reaches GitHub's filtered-search cap")
    starts: dict[int, datetime] = {}
    for page in range(1, (expected + _PER_PAGE - 1) // _PER_PAGE + 1):
        if page > 1:
            count, rows = _page(endpoint + "?" + urlencode({**query, "page": page}), read)
            if count != expected:
                raise ValueError("scheduled window changed while reading pages")
        if not rows or len(rows) > _PER_PAGE:
            raise ValueError("scheduled window page is incomplete")
        for raw in rows:
            row = _start(raw, workflow, branch)
            if row.workflow_id != anchor.workflow_id or not lower <= row.at <= anchor.at:
                raise ValueError("scheduled window contains an unrelated or out-of-window run")
            if row.identifier in starts:
                raise ValueError("scheduled window pages overlap")
            starts[row.identifier] = row.at
    if len(starts) != expected or starts.get(anchor.identifier) != anchor.at:
        raise ValueError("scheduled window coverage cannot be verified")
    return tuple(sorted(starts.values(), reverse=True))
