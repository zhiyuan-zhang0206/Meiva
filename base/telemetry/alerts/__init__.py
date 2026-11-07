"""Alert ingest core — the alerts-store + IM-notification logic.

Alert is fully separate from Notice (Task #1224, user design 2026-08-12: own
table, own UI, own IM channel), and there is one way in: the gateway router
(gateway/alerts/router.py) ingests the Grafana embedded-Alertmanager webhook
on ``POST /api/alerts``. Every rule, grouping, repetition and window silence
is Grafana's (docs/decisions/observability/alerts/2026-10-04-alerting-on-grafana-alerting.md); no other
process writes the ``alerts`` table or pushes an alert to IM.

The functions take the Alertmanager webhook *alert shape as a plain dict*
(status/labels/annotations/starts_at/ends_at/fingerprint/generator_url) so
this module stays below the gateway layer (the events-maintenance
reconciliation shares its key parsing); the router owns the transport.

Dedup key (fingerprint, starts_at): Alertmanager may re-send the same instance
while it is firing and sends it once more on resolution — the
upsert updates the row instead of duplicating it; a resolved row that fires
again (new starts_at) starts a fresh instance. ``fingerprint`` is the
Alertmanager-standard fnv-1a hash over sorted labels; the ingest computes it
when a payload omits it, using the exact Alertmanager algorithm so a
computed hash and a Grafana-sent hash agree for the same label set. The IM
fan-out is gated by the transition logic in ``upsert_alert`` — every severity
pushes, and an open instance pushes again when its severity increases, unless
the rule explicitly labels the alert ``notify_im="false"``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from base.db.rows import IdRow, query_one
from base.telemetry.alerts.copy import (
    ALERT_GROUP_COUNT,
    ALERT_GROUP_MORE,
    ALERT_HEAD,
    ALERT_JUMP_LINK,
    ALERT_LANGUAGE_DEFAULT,
    ALERT_LANGUAGES,
    ALERT_TRIGGERED_AT,
)

_log = logging.getLogger(__name__)

# (fingerprint, starts_at) — the dedup key for one alert instance.
AlertKey = tuple[str, datetime]

# meiva:note
# One alert as the webhook hands it over: a plain JSON object whose fields are read through the
# typed accessors below, never indexed as if they were known strings.
type AlertPayload = dict[str, object]

_STRING_MAP = TypeAdapter(dict[str, str])


# meiva:note
# The label or annotation map of one alert; absent or empty is an empty map, anything that is not a
# string-to-string object is a malformed payload and raises.
def _string_map(alert: AlertPayload, key: str) -> dict[str, str]:
    return _STRING_MAP.validate_python(alert.get(key) or {})


# meiva:note
# The label map of one alert, for the callers that group by a label.
def alert_labels(alert: AlertPayload) -> dict[str, str]:
    return _string_map(alert, "labels")


# meiva:note
# One text field of an alert; absent or empty is ``""``, any other non-string is a malformed
# payload and raises.
def _text(alert: AlertPayload, key: str) -> str:
    value = alert.get(key) or ""
    if not isinstance(value, str):
        raise TypeError(f"alert field {key!r} must be a string")
    return value


# Alertmanager sends the zero time ("0001-01-01T00:00:00Z") for the not-yet-known
# endsAt of a firing alert — treat it as NULL, not a real timestamp.
_ZERO_TS = frozenset({"0001-01-01T00:00:00Z", "0001-01-01T00:00:00+00:00", ""})

# The three severity classes (user design 2026-08-12). Anything else a rule
# label carries normalizes to ``warning`` — the quietest class, so an
# unlabelled rule never reads as an incident.
_SEVERITIES = frozenset({"critical", "warning", "error"})
_SEVERITY_RANK = {"warning": 0, "error": 1, "critical": 2}

# Alertmanager's status vocabulary maps onto the store's: the store has only
# unresolved / resolved (no ack, no escalation — user ruling).
_STATUS_MAP = {"firing": "unresolved", "resolved": "resolved"}

# Instances a grouped message names before it says how many more there are.
_GROUP_LISTED = 3

_IM_GATE_LABEL = "notify_im"
_IM_GATE_VALUE = "false"

# FNV-1a 64-bit constants (the hash Alertmanager fingerprints labels with).
_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3
_FNV_MASK = 0xFFFFFFFFFFFFFFFF


def parse_ts(raw: str) -> datetime | None:
    """RFC3339 -> aware datetime; None for empty / Alertmanager's zero time."""

    if not raw or raw in _ZERO_TS:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def parse_severity(labels: dict[str, str]) -> str:
    """Normalize a rule's severity label to critical/warning/error.

    ``warning`` when absent/unparseable — the quietest default, so an
    unlabelled rule never spams the user."""

    raw = str(labels.get("severity") or "").strip()
    lower = raw.lower()
    return lower if lower in _SEVERITIES else "warning"


def parse_alertname(labels: dict[str, str]) -> str:
    """Human rule title: Alertmanager sets ``alertname`` to the rule name."""

    return labels.get("alertname") or "unknown"


def fingerprint(labels: dict[str, str]) -> str:
    """The Alertmanager-standard fingerprint of a label set: fnv-1a 64-bit
    over each sorted ``name`` + 0xff separator + ``value`` + 0xff separator,
    rendered as the decimal string (Alertmanager's ``Fingerprint().String()``).

    Implemented exactly so a fingerprint the gateway computes for a direct
    writer equals the one Grafana's embedded Alertmanager sends for the same
    labels — the (fingerprint, starts_at) dedup key then stays stable across
    the two paths."""

    h = _FNV_OFFSET
    for name in sorted(labels):
        for chunk in (name.encode(), b"\xff", str(labels[name]).encode(), b"\xff"):
            for byte in chunk:
                h ^= byte
                h = (h * _FNV_PRIME) & _FNV_MASK
    return str(h)


def normalize_status(status: str) -> str:
    """Webhook status (firing/resolved) -> store status (unresolved/resolved)."""

    return _STATUS_MAP.get(status, "unresolved")


def im_fanout_allowed(labels: dict[str, str]) -> bool:
    """Return whether this alert may fan out to IM.

    Only the exact string ``labels["notify_im"] == "false"`` disables IM.
    This conservative gate keeps noisy slow-request rules visible in the UI
    and Insights while keeping the user's IM quiet (Task #1404).
    """

    return labels.get(_IM_GATE_LABEL) != _IM_GATE_VALUE


# meiva:note
# The latest known start of one fingerprint.
@dataclass(frozen=True)
class _StartsAtRow:
    starts_at: datetime


# meiva:note
# The notification state of an instance before this ingest touched it.
@dataclass(frozen=True)
class _PriorAlertRow:
    status: str
    severity: str
    notified_at: datetime | None


# meiva:note
# The stored alert after the upsert, plus whether the statement inserted it. ``labels`` and
# ``annotations`` are jsonb the schema keeps as string maps; the SSE publisher validates them.
@dataclass(frozen=True)
class _StoredAlertRow:
    id: int
    status: str
    severity: str
    alertname: str
    labels: object
    annotations: object
    starts_at: datetime
    ends_at: datetime | None
    fingerprint: str
    generator_url: str
    source: str
    notified_at: datetime | None
    created_at: datetime
    updated_at: datetime
    inserted: bool


# meiva:note
# The IM template language row: ``value`` is jsonb and only a string is a language.
@dataclass(frozen=True)
class _DisplayLanguageRow:
    value: object


def _existing_starts_at(conn: psycopg.Connection, fp: str, alertname: str) -> datetime | None:
    """The latest known starts_at of `fp` for a payload that carries none; None rejects it."""
    # No starts_at (payload drift): re-sends of an EXISTING instance
    # reuse its starts_at so the (fingerprint, starts_at) dedup key stays
    # stable. A genuinely new alert with no starts_at has no stable
    # identity — fabricating now() would make every re-send a fresh
    # instance (duplicate rows + duplicate IMs), so it is rejected.
    row = query_one(
        conn,
        _StartsAtRow,
        "SELECT starts_at FROM alerts WHERE fingerprint = %s ORDER BY starts_at DESC LIMIT 1",
        (fp,),
    )
    if row is not None:
        return row.starts_at
    _log.warning(
        "alerts: alert %r has no starts_at and no existing instance "
        "— rejected (dedup key would be unstable)",
        alertname,
    )
    return None


def _transition_notifies(
    status: str,
    severity: str,
    old_status: str | None,
    old_severity: str | None,
    *,
    was_notified: bool,
) -> bool:
    """The transition rules of the notification gate (see `upsert_alert`)."""
    if status == "unresolved" and (old_status == "resolved" or not was_notified):
        # Firing gate is ``notified_at IS NULL``: keep notifying on every
        # re-send until the message actually lands (was_notified), and on a
        # re-fire after a resolution regardless (new event for the user).
        return True
    if (
        status == "unresolved"
        and old_status == "unresolved"
        and was_notified
        and old_severity is not None
        and _SEVERITY_RANK[severity] > _SEVERITY_RANK[old_severity]
    ):
        # Every firing transition pushes. Escalation on an open instance is
        # new information even though the instance itself was already sent.
        return True
    return old_status == "unresolved" and status == "resolved" and was_notified


def _upsert_row(
    conn: psycopg.Connection,
    alert: AlertPayload,
    *,
    status: str,
    severity: str,
    alertname: str,
    fp: str,
    starts_at: datetime,
    source: str,
) -> tuple[dict[str, object], bool]:
    """INSERT .. ON CONFLICT the instance; the stored row and whether it was a fresh insert."""
    # xmax = 0 distinguishes the fresh INSERT from the ON CONFLICT UPDATE
    # (rowcount is 1 on both paths).
    stored = query_one(
        conn,
        _StoredAlertRow,
        "INSERT INTO alerts"
        " (status, severity, alertname, labels, annotations, starts_at, ends_at,"
        "  fingerprint, generator_url, source)"
        " VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, CASE WHEN %s = 'resolved' THEN now() ELSE NULL END), %s, %s, %s)"
        " ON CONFLICT (fingerprint, starts_at) DO UPDATE SET"
        "  status = EXCLUDED.status,"
        "  severity = EXCLUDED.severity,"
        "  alertname = EXCLUDED.alertname,"
        "  labels = EXCLUDED.labels,"
        "  annotations = EXCLUDED.annotations,"
        "  ends_at = COALESCE(EXCLUDED.ends_at, CASE WHEN EXCLUDED.status = 'resolved' THEN COALESCE(alerts.ends_at, now()) ELSE NULL END),"
        "  generator_url = EXCLUDED.generator_url,"
        "  updated_at = now()"
        " RETURNING id, status, severity, alertname, labels, annotations, starts_at,"
        "           ends_at, fingerprint, generator_url, source, notified_at,"
        "           created_at, updated_at, (xmax = 0) AS inserted",
        (
            status,
            severity,
            alertname,
            Jsonb(_string_map(alert, "labels")),
            Jsonb(_string_map(alert, "annotations")),
            starts_at,
            parse_ts(_text(alert, "ends_at")),
            status,
            fp,
            str(alert.get("generator_url") or ""),
            source,
        ),
    )
    assert stored is not None  # noqa: S101 — upsert always returns a row
    row = asdict(stored)
    del row["inserted"]
    return row, stored.inserted


def alert_instance_fingerprint(alert: AlertPayload) -> str:
    """Use the webhook fingerprint or the established Alertmanager fallback."""
    return _text(alert, "fingerprint") or fingerprint(_string_map(alert, "labels"))


def resolve_alert_key(conn: psycopg.Connection, alert: AlertPayload) -> AlertKey | None:
    """Resolve the existing instance identity without inventing a missing start time."""
    labels = _string_map(alert, "labels")
    fp = alert_instance_fingerprint(alert)
    starts_at = parse_ts(_text(alert, "starts_at"))
    if starts_at is None:
        starts_at = _existing_starts_at(conn, fp, parse_alertname(labels))
    return (fp, starts_at) if starts_at is not None else None


# meiva:note
# Resolve any open alerts for a stopped, completed or deleted schedule.
def resolve_schedule_alerts(conn: psycopg.Connection, schedule_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE alerts SET status = 'resolved', ends_at = COALESCE(ends_at, now()), updated_at = now() "
            "WHERE status = 'unresolved' AND (labels->>'attributes_schedule_id' = %s OR labels->>'schedule_id' = %s)",
            (str(schedule_id), str(schedule_id)),
        )
        return cur.rowcount


@dataclass(frozen=True)
class _ScheduleInactiveRow:
    enabled: bool
    status: str


# meiva:note
# True if the alert targets a schedule that is disabled, stopped, completed, or deleted.
def is_schedule_inactive(conn: psycopg.Connection, labels: dict[str, str]) -> bool:
    sid_str = labels.get("attributes_schedule_id") or labels.get("schedule_id")
    if not sid_str:
        return False
    try:
        sid = int(sid_str)
    except (ValueError, TypeError):
        return False
    has_table = query_one(
        conn,
        IdRow,
        "SELECT 1 AS id FROM pg_tables WHERE schemaname = 'public' AND tablename = 'schedules'",
    )
    if has_table is None:
        return False
    sched_row = query_one(
        conn,
        _ScheduleInactiveRow,
        "SELECT enabled, status FROM schedules WHERE id = %s",
        (sid,),
    )
    if sched_row is None:
        return True
    return (not sched_row.enabled) or (sched_row.status in ("stopped", "completed"))


def upsert_alert(
    conn: psycopg.Connection,
    alert: AlertPayload,
    source: str = "grafana",
    *,
    instance_key: AlertKey | None = None,
) -> tuple[AlertKey, bool, bool, dict[str, object]]:
    """Upsert one alert instance; return (key, did_insert, should_notify, row).

    ``alert`` is the Alertmanager-webhook alert shape as a dict (the model
    dump). ``source`` tags the row's provenance (``grafana``, the webhook's
    default); it is set on insert and never overwritten by a conflict
    re-send — a repeated webhook for the same instance must not rewrite
    history.

    ``should_notify`` — the notification gate (every severity pushes unless
    ``notify_im`` is exactly ``"false"``; transition rules still prevent a
    repeated re-send of a still-firing instance from re-spamming):
    - firing, and the firing has not been IM-notified yet (fresh insert, a
      failed earlier attempt, or a resolved row re-firing): True — a single
      im_bridge outage must not silence the alert forever, the next re-send
      retries while ``notified_at`` stays NULL
    - firing, re-fired after a resolution (a new event for the user): True
    - firing, severity increased on an unresolved already-notified instance:
      True — escalation is a new firing transition and therefore new information
    - instance resolved, and the firing had been IM-notified before: True
    - every other re-send (already firing + already notified, already
      resolved): False
    """

    labels = _string_map(alert, "labels")
    status = normalize_status(str(alert.get("status") or "firing"))
    schedule_inactive = is_schedule_inactive(conn, labels)
    if status == "unresolved" and schedule_inactive:
        status = "resolved"

    severity = parse_severity(labels)
    alertname = parse_alertname(labels)
    key = instance_key or resolve_alert_key(conn, alert)
    if key is None:
        fp = alert_instance_fingerprint(alert)
        return (fp, datetime.min.replace(tzinfo=UTC)), False, False, {}
    fp, starts_at = key

    old = query_one(
        conn,
        _PriorAlertRow,
        "SELECT status, severity, notified_at FROM alerts WHERE fingerprint = %s AND starts_at = %s",
        (fp, starts_at),
    )
    old_status = old.status if old else None
    old_severity = old.severity if old else None
    was_notified = old is not None and old.notified_at is not None

    row, did_insert = _upsert_row(
        conn,
        alert,
        status=status,
        severity=severity,
        alertname=alertname,
        fp=fp,
        starts_at=starts_at,
        source=source,
    )

    should_notify = (
        not schedule_inactive
        and im_fanout_allowed(labels)
        and _transition_notifies(
            status, severity, old_status, old_severity, was_notified=was_notified
        )
    )
    return (fp, starts_at), did_insert, should_notify, row


# meiva:note
# What one IM message renders against, read once where the cluster's configuration is: the zone
# the trigger time is shown in (``None`` when this process holds no authoritative cluster
# timezone, which renders in the host zone) and the fleet-UI base URL the jump link points at
# ('' when no reachable UI is configured).
@dataclass(frozen=True)
class AlertMessageStyle:
    zone: ZoneInfo | None
    base_url: str


# meiva:note
# The zone is a value the caller holds: the gateway root reads the cluster clock once and hands
# it down. A ``None`` zone — this process holding no authoritative cluster timezone
# (settings-lite, gateway down) — renders in the host zone, which is what `Clock.zone()` returns
# there, so the documented lite degradation survives the move.
def format_local(ts: datetime | None, zone: ZoneInfo | None) -> str:
    """Cluster-wall-clock string for the IM message ('' when None).

    Carries year + zone abbreviation (tz audit, 2026-08, PR-6): the prior
    `%m-%d %H:%M` gave no way to tell which year an alert near New Year's
    fired in, and no way to tell which machine's local zone the reader was
    looking at on a multi-machine cluster. Renders in the cluster's
    `MEIVA_TIMEZONE` (user ruling 2026-08-27: one cluster clock — a runner
    whose OS zone differs from the cluster's must not show a different wall
    clock from every other surface); when this process holds no authoritative
    cluster timezone (settings-lite, gateway down) it falls back to the host
    zone, the documented lite degradation.
    """

    if ts is None:
        return ""
    return ts.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")


# meiva:note
# Both inputs arrive as values the gateway root read once for its own use: the gateway root
# already resolves these two URLs for `allowed_origins`, so the derivation happens there rather
# than re-reading the process settings on every render.
def frontend_base_url(gateway_url: str, frontend_healthcheck_url: str) -> str:
    """The user-facing fleet UI base URL for IM jump links.

    The user reaches the fleet UI at Gate's entry port on the
    gateway host (the Next.js app itself binds another port and is proxied).
    Derived from the two existing settings: the HOST of ``MEIVA_GATEWAY_URL``
    (reachable over the private network, never localhost in prod) and the PORT of
    ``MEIVA_FRONTEND_HEALTHCHECK_URL`` (the fleet UI entry the user reaches).
    """

    gw = urlsplit(gateway_url)
    fe = urlsplit(frontend_healthcheck_url)
    host = gw.hostname or fe.hostname
    if not host:
        # No gateway URL and no healthcheck URL configured — no reachable
        # fleet UI to link; never fall back to a loopback address.
        return ""
    port = fe.port or 3000
    return f"{gw.scheme or 'http'}://{host}:{port}"


def _summary(alert: AlertPayload) -> str:
    annotations = _string_map(alert, "annotations")
    return str(annotations.get("summary") or annotations.get("description") or "")


def display_language(conn: psycopg.Connection) -> str:
    """The IM template language — ``user_settings`` key ``display.language``.

    IM copy language follows the UI language (user ruling 2026-08-13: one
    language source, no separate IM field). Returns "zh" | "en"; a missing
    row or an unknown value falls back to ``ALERT_LANGUAGE_DEFAULT`` ("zh").
    Only template/framework copy is translated — alert data never is.
    """

    row = query_one(
        conn, _DisplayLanguageRow, "SELECT value FROM user_settings WHERE key = 'display.language'"
    )
    raw = row.value if row else None
    return raw if isinstance(raw, str) and raw in ALERT_LANGUAGES else ALERT_LANGUAGE_DEFAULT


def notify_text(alert: AlertPayload, lang: str | None = None, *, style: AlertMessageStyle) -> str:
    """The alert IM message — the alert-specific format (user design 2026-08-12):
    a warning-sign-prefixed ``ALERT [<severity>] <alertname>`` head line, the
    summary body, the generatorURL when present, the trigger time, and the
    fleet-UI jump link (omitted when no fleet-UI base URL is configured).
    Resolved instances swap the head for the check-marked ``RESOLVED``
    variant. Every severity pushes
    (critical/warning/error — no severity gate).

    Templates live in ``base/telemetry/alerts/copy.py`` — the single source of
    user-visible IM copy (governance ruling 2026-08-08; moved down from
    services/entrypoints/im_bridge/copy.py by the 2026-08-25 tech-audit P1 so shared does
    not import up into services). ``lang`` picks the template language
    ("zh" | "en"); ``None`` (or an unknown value) falls back to
    ``ALERT_LANGUAGE_DEFAULT`` ("zh", user ruling 2026-08-13). Alert
    labels/annotations data (severity, alertname, summary, generator_url)
    passes through untranslated.
    """

    labels = _string_map(alert, "labels")
    severity = parse_severity(labels).upper()
    alertname = parse_alertname(labels)
    resolved = normalize_status(str(alert.get("status") or "")) == "resolved"
    lang = lang if lang in ALERT_LANGUAGES else ALERT_LANGUAGE_DEFAULT
    head = ALERT_HEAD[lang]["resolved" if resolved else "firing"].format(
        severity=severity, alertname=alertname
    )
    lines = [head]
    summary = _summary(alert)
    if summary:
        lines.append(summary[:200])
    generator_url = _text(alert, "generator_url")
    if generator_url:
        lines.append(generator_url)
    start = format_local(parse_ts(_text(alert, "starts_at")), style.zone)
    if start:
        lines.append(ALERT_TRIGGERED_AT[lang].format(time=start))
    if style.base_url:
        lines.append(ALERT_JUMP_LINK.format(url=style.base_url))
    return "\n".join(lines)


def notify_group_text(
    alerts: list[AlertPayload], lang: str | None = None, *, style: AlertMessageStyle
) -> str:
    """One IM message for the instances of one rule that a single webhook POST carries.

    Grafana groups a rule's instances into one notification (`group_by alertname`), so the
    user hears one message per group, not one per instance: a lone instance keeps the
    ``notify_text`` format; several share one head (the highest severity, the alertname and
    ``xN``), list the first few summaries and count the rest. The store still holds one row
    per instance. Instances are expected to share their alertname and status."""

    if len(alerts) == 1:
        return notify_text(alerts[0], lang, style=style)
    lang = lang if lang in ALERT_LANGUAGES else ALERT_LANGUAGE_DEFAULT
    lines = [_group_head(alerts, lang)]
    lines += [f"- {s[:200]}" for a in alerts[:_GROUP_LISTED] if (s := _summary(a))]
    if len(alerts) > _GROUP_LISTED:
        lines.append(ALERT_GROUP_MORE[lang].format(n=len(alerts) - _GROUP_LISTED))
    starts = [t for a in alerts if (t := parse_ts(_text(a, "starts_at"))) is not None]
    if starts:
        lines.append(ALERT_TRIGGERED_AT[lang].format(time=format_local(min(starts), style.zone)))
    if style.base_url:
        lines.append(ALERT_JUMP_LINK.format(url=style.base_url))
    return "\n".join(lines)


def _group_head(alerts: list[AlertPayload], lang: str) -> str:
    """The grouped message's head: highest severity, the alertname and the count."""

    labels_of = [_string_map(a, "labels") for a in alerts]
    severity = max((parse_severity(lab) for lab in labels_of), key=_SEVERITY_RANK.__getitem__)
    resolved = normalize_status(str(alerts[0].get("status") or "")) == "resolved"
    head = ALERT_HEAD[lang]["resolved" if resolved else "firing"].format(
        severity=severity.upper(), alertname=parse_alertname(labels_of[0])
    )
    return head + ALERT_GROUP_COUNT.format(n=len(alerts))
