"""Native alert source facts and revision-fenced completion, without transport ownership."""

from dataclasses import asdict, dataclass
from enum import StrEnum

from psycopg import Connection

from base.db.rows import count_one, query_all, query_one


class AlertGroupOrigin(StrEnum):
    SHADOW = "shadow"
    NATIVE = "native-v1"


class NativeAlertSourceError(ValueError):
    """A requested group does not carry the native immutable source contract."""


# meiva:note
# One stored notification group, exactly the columns the native source contract names.
@dataclass(frozen=True)
class _GroupRow:
    id: int
    status: str
    alertname: str
    language: str
    render_version: str
    text: str
    origin: str


# meiva:note
# One member of a notification group, in the order it was frozen.
@dataclass(frozen=True)
class _MemberRow:
    alert_id: int
    notification_revision: int
    ordinal: int
    reason: str


# meiva:note
# The group as a JSON object (its columns plus ``members``, a list of member objects), which is
# the shape the IM outbound request retains.
def load_native_group(conn: Connection, group_id: int, *, lock: bool = False) -> dict[str, object]:
    if type(group_id) is not int or not 0 < group_id < 2**63:
        raise NativeAlertSourceError("a positive alert group ID is required")
    group = query_one(
        conn,
        _GroupRow,
        "SELECT id,status,alertname,language,render_version,text,origin "
        "FROM alert_notification_groups WHERE id=%s" + (" FOR UPDATE" if lock else ""),
        (group_id,),
    )
    if group is None or group.origin != AlertGroupOrigin.NATIVE:
        raise NativeAlertSourceError("alert group has no native source origin")
    members = query_all(
        conn,
        _MemberRow,
        "SELECT alert_id,notification_revision,ordinal,reason FROM alert_notification_members "
        "WHERE group_id=%s ORDER BY ordinal",
        (group_id,),
    )
    if not members or any(member.reason == "legacy_unconfirmed" for member in members):
        raise NativeAlertSourceError("native alert group has an unqualified membership")
    if group.render_version != "alert-group-v1":
        raise NativeAlertSourceError("unsupported native alert render version")
    return {**asdict(group), "members": [asdict(member) for member in members]}


def stamp_native_sent(conn: Connection, group_id: int) -> int:
    """Any real channel SENT completes only the current source revision/status.

    notified_at retains its existing first-ever timestamp semantics. A legacy
    timestamp cannot populate notified_revision, which is native SENT evidence.
    """
    return conn.execute(
        "UPDATE alerts a SET notified_revision=m.notification_revision, "
        "notified_at=COALESCE(a.notified_at,now()) "
        "FROM alert_notification_members m JOIN alert_notification_groups g ON g.id=m.group_id "
        "WHERE g.id=%s AND g.origin='native-v1' AND a.id=m.alert_id "
        "AND a.notification_revision=m.notification_revision AND a.status=g.status "
        "AND a.notified_revision<m.notification_revision",
        (group_id,),
    ).rowcount


def native_sent_count(conn: Connection, group_ids: set[int]) -> int:
    return count_one(
        conn,
        "SELECT count(*) AS n FROM alert_notification_members m JOIN alerts a ON a.id=m.alert_id "
        "JOIN alert_notification_groups g ON g.id=m.group_id "
        "WHERE g.origin='native-v1' AND a.status=g.status AND m.group_id=ANY(%s) AND a.notification_revision=m.notification_revision "
        "AND a.notified_revision=m.notification_revision",
        (list(group_ids),),
    )
