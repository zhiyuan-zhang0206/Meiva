from datetime import UTC, datetime
from shlex import join

from gateway.alerts.schemas import AlertWebhookAlert, AlertWebhookPayload, WebhookStatus

_TRIAGE_LABELS = ("agent_id", "machine", "machine_name", "route", "event_name")


def notification_text(payload: AlertWebhookPayload, *, now: datetime) -> str:
    lines = [f"Alertmanager notification ({len(payload.alerts)} alert instance(s))"]
    for alert in payload.alerts:
        status = (
            alert.status if "status" in alert.model_fields_set else payload.status or alert.status
        )
        lines.append("")
        lines.append(
            f"[{status}] [{alert.labels.get('severity', 'unlabeled')}] "
            f"{alert.labels.get('alertname') or 'unknown'}"
        )
        if summary := alert.annotations.get("summary"):
            lines.append(summary)
        if labels := [
            f"{name}={alert.labels[name]}" for name in _TRIAGE_LABELS if alert.labels.get(name)
        ]:
            lines.append("Labels: " + ", ".join(labels))
        if alert.generator_url:
            lines.append(f"Grafana: {alert.generator_url}")
        lines.append(f"Events: `{_events_command(alert, status=status, now=now)}`")
    return "\n".join(lines)


def _events_command(alert: AlertWebhookAlert, *, status: WebhookStatus, now: datetime) -> str:
    command = ["meiva", "events", "ls"]
    start = _parse_ts(alert.starts_at)
    recovered_at = _parse_ts(alert.ends_at) if status == "resolved" else None
    end = recovered_at or now
    if start is not None and start < end:
        command += ["--from", start.isoformat(), "--to", end.isoformat()]
    agent_id = alert.labels.get("agent_id", "")
    if agent_id.isascii() and agent_id.isdecimal():
        command.append(f"--agent={agent_id}")
    if machine := alert.labels.get("machine") or alert.labels.get("machine_name"):
        command.append(f"--machine={machine}")
    if event := alert.labels.get("event_name"):
        command.append(f"--name={event}")
    return join(command)


_ZERO_TS = frozenset({"0001-01-01T00:00:00Z", "0001-01-01T00:00:00+00:00", ""})


def _parse_ts(raw: str) -> datetime | None:
    if raw in _ZERO_TS:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
