"""Permanent session history and structured session-record files.

The UUID is a private reference retained for pre-upgrade checkpoint receipts.
Every public handle, path and display uses the agent-scoped integer session id.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NotRequired, TypedDict
from uuid import UUID

from psycopg.rows import class_row, dict_row
from psycopg.types.json import Jsonb

from base.agents.impersonation._store import machine_mismatch_message
from base.agents.impersonation.status import (
    ImpersonationMetadata,
    ImpersonationStatus,
    LeaseColumns,
    LeaseRecord,
    PublicLease,
    parse_lease,
)
from base.db.protocols import DatabaseLike, SyncConnection
from base.events.live.bus import EventBus
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from base.host.private_storage import write_private_bytes

# A consumed event's durable identity is content parsed from the original log line.
# Reader-synthesized projections (event id, line_sha256, tier, or future additions)
# stay outside this tuple; add a field only when it becomes producer content.
_EVENT_CONTENT_FIELDS: tuple[str, ...] = (
    "ts",
    "trace_id",
    "span_id",
    "agent_id",
    "machine",
    "process",
    "category",
    "event_name",
    "level",
    "source",
    "target_agent_id",
    "attributes",
)
_CONSUMED_EVENT_KINDS: tuple[str, ...] = ("sdk_call", "api_event")


# meiva:note
# One row of a lease's log: what `entries` returns and the handoff document is made of.
class Entry(TypedDict):
    seq: int
    kind: str
    created_at: datetime
    payload: dict[str, object]


# meiva:note
# A log entry of kind `message`; an incoming one also says whether the session acknowledged it.
class Message(Entry):
    acknowledged: NotRequired[bool]


# meiva:note
# The session handle as the history API and the handoff document show it: the numeric id, no
# UUID and no credential. A lease that is still being prepared reads as `preparing`.
class PublicSession(TypedDict):
    id: int
    agent_id: int
    session_id: int
    name: str
    executor_name: str
    process_metadata: dict[str, object]
    status: ImpersonationStatus | Literal["preparing"]
    created_at: datetime
    activated_at: datetime | None
    ended_at: datetime | None
    expires_at: datetime
    ttl_seconds: int
    ack_window_seconds: int
    max_delivery_attempts: int
    reason: str
    summary: str | None
    handoff_path: str | None
    handoff_applied_at: datetime | None
    rejection_reason: str | None
    relay_provider: str | None
    relay_heartbeat_at: datetime | None
    relay_last_failure_at: datetime | None
    events_completed_at: datetime | None


# meiva:note
# What the log shows of the session's SDK calls, over the consumed events only.
class _SdkStatistics(TypedDict):
    sdk_calls: dict[str, int]
    sdk_statistics_basis: str
    sdk_sampling_policy: str
    sdk_duration_seconds: float


# meiva:note
# What the log shows of the session's API operations.
class _ApiStatistics(TypedDict):
    api_operations: dict[str, int]
    message_recipients: dict[str, int]
    task_ids_created: list[int]
    task_ids_updated: list[int]
    tasks_created: int
    tasks_updated: int


# meiva:note
# How completely one event stream of the log covers the emitted events.
class _Coverage(TypedDict):
    coverage: str
    consumed_event_count: int


# meiva:note
# The SDK stream's coverage also names why no count of calls can be read from it.
class _SdkCoverage(_Coverage):
    sampling_policy: str


# meiva:note
# Whether the handoff's event log is complete for the emitted events, and why not.
class EventDelivery(TypedDict):
    state: str
    pending_reason: str | None
    completion_basis: str | None
    sdk_calls: _SdkCoverage
    api_events: _Coverage


# meiva:note
# The derived counts of a handoff document.
class Statistics(_SdkStatistics, _ApiStatistics):
    duration_seconds: float | None
    event_delivery: EventDelivery
    incoming_messages: int
    outgoing_messages: int


# meiva:note
# The handoff document built from a lease and its log (version 2).
class HandoffDocument(TypedDict):
    version: int
    session: PublicSession
    messages: list[Message]
    lifecycle: list[Entry]
    sdk_events: list[Entry]
    api_events: list[Entry]
    statistics: Statistics


# meiva:note
# The columns of one log entry as the database stores them; the payload is still to be checked.
@dataclass(frozen=True)
class _EntryRow:
    seq: int
    kind: str
    created_at: datetime
    payload: object


# meiva:note
# An entry already recorded under an event key.
@dataclass(frozen=True)
class _RecordedEntry:
    seq: int
    payload: object


# meiva:note
# The sequence number `append` took from the lease's counter.
@dataclass(frozen=True)
class _SeqRow:
    seq: int


# meiva:note
# One pending inbox row handed to a session at activation.
@dataclass(frozen=True)
class _PendingInbound:
    id: int
    content: str
    kind: str
    source: str
    payload: object
    created_at: datetime


# meiva:note
# The central audit record an event reference names.
@dataclass(frozen=True)
class _AuditBody:
    event_uid: int
    ts: datetime
    trace_id: str | None
    span_id: str | None
    agent_id: int | None
    machine: str
    process: str
    event_name: str
    level: str
    source: str
    target_agent_id: int | None
    attributes: object


# meiva:note
# A log payload is a JSON object; anything else is a corrupt row.
def _object_payload(value: object) -> dict[str, object]:
    if not is_object_dict(value):
        raise TypeError("An impersonation log payload must be a JSON object")
    return value


# meiva:note
# A payload field the statistics read as text.
def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("An impersonation log field must be text")
    return value


# meiva:note
# A payload field the statistics read as an integer.
def _whole(value: object) -> int:
    if not isinstance(value, int):
        raise TypeError("An impersonation log field must be an integer")
    return value


# meiva:note
# A payload field the statistics read as a number.
def _number(value: object) -> int | float:
    if not isinstance(value, (int, float)):
        raise TypeError("An impersonation log field must be a number")
    return value


def metadata(lease: Mapping[str, object]) -> ImpersonationMetadata:
    return ImpersonationMetadata.model_validate(
        {
            "agent_id": lease["agent_id"],
            "session_id": lease["session_id"],
            "name": lease["name"],
            "executor_name": lease["executor_name"],
            "provider": lease["relay_provider"],
        }
    )


def public_session(lease: LeaseColumns) -> PublicSession:
    """Expose the numeric handle without legacy UUIDs or credentials."""
    status = ImpersonationStatus(lease["status"])
    return PublicSession(
        id=lease["session_id"],
        agent_id=lease["agent_id"],
        session_id=lease["session_id"],
        name=lease["name"],
        executor_name=lease["executor_name"],
        process_metadata=lease["process_metadata"],
        status=(
            "preparing"
            if status in (ImpersonationStatus.REQUESTED, ImpersonationStatus.ACCEPTED)
            else status
        ),
        created_at=lease["created_at"],
        activated_at=lease["activated_at"],
        ended_at=lease["ended_at"],
        expires_at=lease["expires_at"],
        ttl_seconds=lease["ttl_seconds"],
        ack_window_seconds=lease["ack_window_seconds"],
        max_delivery_attempts=lease["max_delivery_attempts"],
        reason=lease["reason"],
        summary=lease["summary"],
        handoff_path=lease["handoff_path"],
        handoff_applied_at=lease["handoff_applied_at"],
        rejection_reason=lease["rejection_reason"],
        relay_provider=lease["relay_provider"],
        relay_heartbeat_at=lease["relay_heartbeat_at"],
        relay_last_failure_at=lease["relay_last_failure_at"],
        events_completed_at=lease["events_completed_at"],
    )


def resolve(
    db: DatabaseLike,
    agent_id: int,
    session_id: int,
    *,
    local_machine: str,
) -> LeaseRecord:
    if isinstance(session_id, bool) or not isinstance(session_id, int) or session_id < 0:
        raise ValueError("session_id must be a nonnegative integer")
    with db.connect() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM agent_impersonations WHERE agent_id=%s AND session_id=%s",
            (agent_id, session_id),
        )
        lease = cur.fetchone()
    if lease is None:
        raise ValueError(f"Agent {agent_id} has no impersonation session {session_id}")
    parsed = parse_lease(lease)
    if parsed["machine"] != local_machine:
        raise ValueError(
            machine_mismatch_message(
                agent_id=agent_id,
                agent_machine=parsed["machine"],
                local_machine=local_machine,
            )
        )
    return parsed


def set_actor(conn: SyncConnection, actor: str) -> None:
    conn.execute("SELECT set_config('meiva.impersonation_actor',%s,true)", (actor,))


def _event_content(payload: Mapping[str, object]) -> tuple[object, ...]:
    return tuple(payload.get(field) for field in _EVENT_CONTENT_FIELDS)


def append(
    conn: SyncConnection,
    lease_id: str,
    kind: str,
    payload: Mapping[str, object],
    *,
    event_key: str | None = None,
    created_at: datetime | None = None,
    source_key: str | None = None,
) -> int:
    """Append under the lease lock; repeat event identities return the original row."""
    if event_key is not None:
        with conn.cursor(row_factory=class_row(_RecordedEntry)) as cur:
            existing = cur.execute(
                "SELECT seq,payload FROM agent_impersonation_entries "
                "WHERE lease_id=%s AND event_key=%s",
                (lease_id, event_key),
            ).fetchone()
        if existing is not None:
            same_content = (
                _event_content(_resolve_payload(conn, _object_payload(existing.payload)))
                == _event_content(_resolve_payload(conn, payload))
                if kind in _CONSUMED_EVENT_KINDS
                else existing.payload == payload
            )
            if not same_content:
                raise ValueError("Event key already belongs to different content")
            return existing.seq
    with conn.cursor(row_factory=class_row(_SeqRow)) as cur:
        row = cur.execute(
            "UPDATE agent_impersonations SET next_entry=next_entry+1 WHERE id=%s "
            "RETURNING next_entry-1 AS seq",
            (lease_id,),
        ).fetchone()
    if row is None:
        raise ValueError("Impersonation session does not exist")
    seq = row.seq
    conn.execute(
        "INSERT INTO agent_impersonation_entries(lease_id,seq,kind,event_key,created_at,payload,"
        "source_key) VALUES(%s,%s,%s,%s,COALESCE(%s,clock_timestamp()),%s,%s)",
        (lease_id, seq, kind, event_key, created_at, Jsonb(payload), source_key),
    )
    return seq


def capture_pending(conn: SyncConnection, lease: LeaseRecord | PublicLease) -> None:
    """Include the backlog handed to this session, even if it predates activation."""
    with conn.cursor(row_factory=class_row(_PendingInbound)) as cur:
        rows = cur.execute(
            "SELECT id,content,kind,source,payload,created_at FROM inbound_messages "
            "WHERE agent_id=%s AND status='pending' AND kind IN ('chat','system_note','cancel','reminder','heartbeat') "
            "ORDER BY id",
            (lease["agent_id"],),
        ).fetchall()
    for row in rows:
        append(
            conn,
            str(lease["id"]),
            "message",
            {
                "direction": "in",
                "inbound_id": row.id,
                "kind": row.kind,
                "source": row.source,
                "content": row.content,
                "payload": row.payload,
            },
            event_key=f"inbound:{row.id}",
            created_at=row.created_at,
        )


def say(
    db: DatabaseLike,
    bus: EventBus,
    lease_id: str,
    content: str,
    *,
    phase: str = "commentary",
    message_key: str,
    local_machine: str,
) -> int:
    """Commit a user-visible reply before publishing its refresh notification."""
    from base.agents.impersonation._store import lock_lease, require_active_locked
    from base.events.live.projection import ImpersonationChanged

    if not content.strip() or phase not in ("commentary", "final") or not message_key.strip():
        raise ValueError("A message needs nonempty content/key and commentary or final phase")
    with db.write_transaction() as conn:
        lease = lock_lease(conn, lease_id)
        require_active_locked(
            conn,
            lease,
            local_machine=local_machine,
        )
        seq = append(
            conn,
            lease_id,
            "message",
            {
                "direction": "out",
                "source": f"agent:{lease['agent_id']}",
                "content": content,
                "phase": phase,
                "impersonation": metadata(lease).model_dump(),
            },
            event_key=f"reply:{message_key}",
        )
        if seq >= lease["next_entry"]:
            conn.execute(
                "UPDATE agents_meta SET last_message_text=%s,last_active_at=clock_timestamp() WHERE id=%s",
                (content, lease["agent_id"]),
            )
    bus.publish_best_effort_sync(
        ImpersonationChanged(agent_id=lease["agent_id"]).model_dump_json(),
        context="impersonation_message",
    )
    return seq


def entries(lease_id: str, conn: SyncConnection) -> list[Entry]:
    """The lease's log in order, central event references resolved to their bodies."""
    with conn.cursor(row_factory=class_row(_EntryRow)) as cur:
        rows = cur.execute(
            "SELECT seq,kind,created_at,payload FROM agent_impersonation_entries "
            "WHERE lease_id=%s ORDER BY seq",
            (lease_id,),
        ).fetchall()
    log = [
        Entry(
            seq=row.seq,
            kind=row.kind,
            created_at=row.created_at,
            payload=_object_payload(row.payload),
        )
        for row in rows
    ]
    _resolve_event_references(log, conn)
    return log


def _audit_bodies(conn: SyncConnection, uids: list[int]) -> dict[int, _AuditBody]:
    with conn.cursor(row_factory=class_row(_AuditBody)) as cur:
        bodies = cur.execute(
            "SELECT event_uid,ts,trace_id,span_id,agent_id,machine,process,event_name,level,"
            "source,target_agent_id,attributes FROM audit_events WHERE event_uid = ANY(%s)",
            (uids,),
        ).fetchall()
    return {body.event_uid: body for body in bodies}


def _resolved_payload(
    reference: Mapping[str, object], body: _AuditBody | None
) -> dict[str, object]:
    """The event payload a central reference names; a missing row is a broken invariant.

    The record is written in the same transaction as the reference, so it is
    never legitimately absent.
    """
    if body is None:
        raise RuntimeError(f"audit_events has no row for event_uid {reference['event_uid']}")
    return {
        "ts": body.ts.astimezone(UTC).isoformat(),
        "trace_id": body.trace_id,
        "span_id": body.span_id,
        "agent_id": body.agent_id,
        "machine": body.machine,
        "process": body.process,
        "category": "audit",
        "event_name": body.event_name,
        "level": body.level,
        "source": body.source,
        "target_agent_id": body.target_agent_id,
        "attributes": body.attributes,
        "id": reference["id"],
        "line_sha256": reference["line_sha256"],
        "event_uid": reference["event_uid"],
    }


def _event_uid(payload: Mapping[str, object]) -> int:
    return _whole(payload["event_uid"])


def _resolve_event_references(rows: list[Entry], conn: SyncConnection) -> None:
    """Replace each central reference payload with the `audit_events` body it names."""
    refs = [row for row in rows if "event_uid" in row["payload"]]
    if not refs:
        return
    bodies = _audit_bodies(conn, [_event_uid(row["payload"]) for row in refs])
    for row in refs:
        row["payload"] = _resolved_payload(row["payload"], bodies.get(_event_uid(row["payload"])))


def _resolve_payload(conn: SyncConnection, payload: Mapping[str, object]) -> Mapping[str, object]:
    if "event_uid" not in payload:
        return payload
    return _resolved_payload(
        payload, _audit_bodies(conn, [_event_uid(payload)]).get(_event_uid(payload))
    )


def _json_default(value: object) -> str:
    if isinstance(value, (datetime, UUID, Path)):
        return str(value) if not isinstance(value, datetime) else value.isoformat()
    raise TypeError(f"Unsupported history value: {type(value).__name__}")


def event_belongs_to_agent(event: Mapping[str, object], agent_id: int) -> bool:
    """Audit source is the actor; send_message's primary agent is its recipient."""
    if event["event_name"] == "sdk_call":
        return event["agent_id"] == agent_id
    source = event["source"]
    return source == f"agent:{agent_id}" or (source == "self" and event["agent_id"] == agent_id)


def _messages_with_ack(rows: list[Entry]) -> list[Message]:
    acknowledged: set[object] = set()
    for row in rows:
        if row["kind"] == "lifecycle" and row["payload"].get("event") == "ack":
            message_ids = row["payload"]["message_ids"]
            if not is_object_list(message_ids):
                raise TypeError("An ack entry must list its message ids")
            acknowledged.update(message_ids)
    messages: list[Message] = []
    for row in rows:
        if row["kind"] != "message":
            continue
        message = Message(
            seq=row["seq"], kind=row["kind"], created_at=row["created_at"], payload=row["payload"]
        )
        if row["payload"]["direction"] == "in":
            message["acknowledged"] = (
                row["payload"]["inbound_id"] in acknowledged
                or row["payload"].get("acknowledged_at") is not None
            )
        messages.append(message)
    return messages


def _attributes(event: Mapping[str, object]) -> dict[str, object]:
    attributes = event["attributes"]
    if not is_object_dict(attributes):
        raise TypeError("An event's attributes must be a JSON object")
    return attributes


def _api_statistics(api: list[Entry], agent_id: int) -> _ApiStatistics:
    own = [row["payload"] for row in api if event_belongs_to_agent(row["payload"], agent_id)]
    counts = Counter(_text(event["event_name"]) for event in own)
    recipients = Counter(
        str(event["agent_id"]) for event in own if event["event_name"] == "send_message"
    )
    created = sorted(
        {
            _whole(_attributes(event)["task_id"])
            for event in own
            if event["event_name"] == "task_create"
        }
    )
    updated = sorted(
        {
            _whole(_attributes(event)["task_id"])
            for event in own
            if event["event_name"] == "task_update"
        }
    )
    return _ApiStatistics(
        api_operations=dict(sorted(counts.items())),
        message_recipients=dict(sorted(recipients.items())),
        task_ids_created=created,
        task_ids_updated=updated,
        tasks_created=len(created),
        tasks_updated=len(updated),
    )


def _sdk_statistics(sdk: list[Entry]) -> _SdkStatistics:
    attributes = [_attributes(row["payload"]) for row in sdk]
    calls = Counter(_text(item["fn"]) for item in attributes)
    return _SdkStatistics(
        sdk_calls=dict(sorted(calls.items())),
        sdk_statistics_basis="consumed_events",
        sdk_sampling_policy="unknown",
        sdk_duration_seconds=sum(_number(item["duration"]) for item in attributes),
    )


def _event_delivery_statistics(
    lease: LeaseColumns, sdk: list[Entry], api: list[Entry]
) -> EventDelivery:
    """Describe whether the handoff's event log is complete for the emitted events.

    The database completes a log-native lease once it has ended and every source has
    sealed. SDK sampling policy is not certified per session, so zero
    consumed SDK events is never evidence of zero SDK calls.
    """
    complete = lease["events_completed_at"] is not None
    coverage = "complete_emitted_events" if complete else "unknown"
    return EventDelivery(
        state="complete" if complete else "pending",
        pending_reason=None if complete else _pending_delivery_reason(lease),
        completion_basis="source_log" if complete else None,
        sdk_calls=_SdkCoverage(
            coverage=coverage, sampling_policy="unknown", consumed_event_count=len(sdk)
        ),
        api_events=_Coverage(coverage=coverage, consumed_event_count=len(api)),
    )


def _pending_delivery_reason(lease: LeaseColumns) -> str:
    from base.agents.impersonation.status import pending_reason

    result = pending_reason(lease)
    if result is None:
        raise RuntimeError("A pending delivery must have a diagnostic reason")
    return result


def _by_occurrence(rows: list[Entry]) -> list[Entry]:
    """The log returns events in append order (late arrivals after);
    the handoff reader needs call order. ``created_at`` is the event's own time."""
    return sorted(rows, key=lambda row: (row["created_at"], row["seq"]))


def build_document(lease: LeaseColumns, rows: list[Entry]) -> HandoffDocument:
    """Derive counts only from recorded facts; preserve the original events too."""
    sdk = _by_occurrence([row for row in rows if row["kind"] == "sdk_call"])
    api = _by_occurrence([row for row in rows if row["kind"] == "api_event"])
    messages = _messages_with_ack(rows)
    directions = Counter(row["payload"]["direction"] for row in messages)
    started, ended = lease["activated_at"] or lease["created_at"], lease["ended_at"]
    return HandoffDocument(
        version=2,
        session=public_session(lease),
        messages=messages,
        lifecycle=[row for row in rows if row["kind"] == "lifecycle"],
        sdk_events=sdk,
        api_events=api,
        statistics=Statistics(
            **_sdk_statistics(sdk),
            **_api_statistics(api, lease["agent_id"]),
            duration_seconds=(ended - started).total_seconds() if ended is not None else None,
            event_delivery=_event_delivery_statistics(lease, sdk, api),
            incoming_messages=directions["in"],
            outgoing_messages=directions["out"],
        ),
    )


# meiva:note
# Write one JSON file atomically in `workspace`, before native resumption.
def export_handoff(
    lease: LeaseRecord,
    conn: SyncConnection,
    *,
    workspace: Path,
) -> tuple[dict[str, object], str]:
    target = workspace / "impersonation" / f"{lease['session_id']}.json"
    stored = lease["handoff_document"]
    if is_object_dict(stored) and stored["version"] == 2:
        session = stored["session"]
        if not is_object_dict(session):
            raise TypeError("A stored handoff document must hold its session as an object")
        session["handoff_path"] = str(target)
        encoded = json.dumps(stored, ensure_ascii=False, indent=2, default=_json_default) + "\n"
    else:
        document = build_document(lease, entries(str(lease["id"]), conn))
        document["session"]["handoff_path"] = str(target)
        encoded = json.dumps(document, ensure_ascii=False, indent=2, default=_json_default) + "\n"
    write_private_bytes(target, encoded.encode("utf-8"))
    decoded = object_of(json.loads, encoded)
    if not is_object_dict(decoded):
        raise TypeError("A handoff document encodes a JSON object")
    return decoded, str(target)
