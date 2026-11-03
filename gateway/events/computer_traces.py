"""Computer-use trace endpoints — /api/computer/traces.

One task's desktop-action trail, assembled from the unified events stream:
computer_session_start / computer_action / computer_session_end rows whose
`attributes.task_id` matches (Phase 2, task #1101). The replay page and any
audit consumer read one endpoint instead of hand-joining three event names.

These are audit events, read from `audit_events` with no time window (the
record is permanent), and a trace is always a small bounded read (500 rows).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from base.events.reads import audit_rows
from gateway.http.config import gateway_state

router = APIRouter()

_TRACE_EVENT_NAMES = (
    "computer_action",
    "computer_session_start",
    "computer_session_end",
)

# A trace is bounded: 500 rows covers long sessions without unbounded scans.
_TRACE_LIMIT = 500


@router.get("/api/computer/traces")
def get_computer_trace(
    request: Request,
    task_id: int = Query(..., ge=1, description="task id whose desktop trail to read"),
) -> dict[str, object]:
    """The desktop-action trail for one task: session envelope + actions.

    Returns:
      {task_id, actions: [{id, ts, agent_id, event, action, app, outcome,
        coords, path, error}...], start: {...}|null, end: {...}|null}
    — chronological, one entry per event row. `start`/`end` are the session
    envelope rows (may be null while a session is open); `actions` carries
    the computer_action rows with the replay-relevant payload keys.
    """
    with gateway_state(request).db_pool.connection() as conn:
        rows, _has_more = audit_rows.query_events(
            conn,
            attribute_filters={"task_id": str(task_id)},
            event_names=list(_TRACE_EVENT_NAMES),
            limit=_TRACE_LIMIT,
            direction="forward",
        )
    if not rows:
        raise HTTPException(status_code=404, detail=f"no computer-use trace for task {task_id}")
    actions: list[dict[str, object]] = []
    start: dict[str, object] | None = None
    end: dict[str, object] | None = None
    for row in rows:
        attrs = row["attributes"]
        entry: dict[str, object] = {
            "id": row["id"],
            "ts": row["ts"].isoformat(),
            "agent_id": row["agent_id"],
            "event": row["event_name"],
        }
        if row["event_name"] == "computer_action":
            entry.update(
                {
                    "action": attrs.get("action"),
                    "app": attrs.get("app"),
                    "outcome": attrs.get("outcome"),
                    "coords": attrs.get("coords"),
                    "path": attrs.get("path"),
                    "error": attrs.get("error"),
                }
            )
            actions.append(entry)
        elif row["event_name"] == "computer_session_start":
            start = entry | {
                "first_tool": attrs.get("first_tool"),
                "first_action_at": attrs.get("first_action_at"),
            }
        else:  # computer_session_end
            end = entry | {
                "action_count": attrs.get("action_count"),
                "first_action_at": attrs.get("first_action_at"),
                "last_action_at": attrs.get("last_action_at"),
                "outcome": attrs.get("outcome"),
            }
    return {"task_id": task_id, "start": start, "end": end, "actions": actions}
