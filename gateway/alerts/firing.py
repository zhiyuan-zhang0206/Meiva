from __future__ import annotations

import logging
from collections import Counter

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from base.host.boundary.untyped import object_of
from gateway.http.config import GrafanaProxy
from gateway.schemas.alerts import FiringAlerts

_log = logging.getLogger(__name__)

ACTIVE_ALERTS_PATH = "/grafana/api/alertmanager/grafana/api/v2/alerts"
ACTIVE_ONLY = {"active": "true", "silenced": "false", "inhibited": "false"}
READ_TIMEOUT = httpx.Timeout(2.0)
WATCHDOG_METRIC = "alert_pipeline_watchdog"
SEVERITIES = frozenset({"critical", "error", "warning"})


class ActiveAlert(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    labels: dict[str, str]


_SNAPSHOT = TypeAdapter(list[ActiveAlert])


class GrafanaSnapshotError(ValueError):
    pass


def count_firing(payload: object) -> FiringAlerts:
    try:
        entries = _SNAPSHOT.validate_python(payload, strict=True)
    except ValidationError as exc:
        raise GrafanaSnapshotError(
            "Grafana active-alert response must be a list of objects"
        ) from exc
    by_severity: Counter[str] = Counter()
    for entry in entries:
        if entry.labels.get("metric") == WATCHDOG_METRIC:
            continue
        severity = entry.labels.get("severity", "").strip().lower()
        by_severity[severity if severity in SEVERITIES else "warning"] += 1
    return FiringAlerts(
        state="ok",
        firing=by_severity.total(),
        critical=by_severity["critical"],
        error=by_severity["error"],
        warning=by_severity["warning"],
    )


async def read_firing_alerts(client: httpx.AsyncClient, grafana: GrafanaProxy) -> FiringAlerts:
    if not grafana.enabled:
        return FiringAlerts(state="not_configured")
    try:
        response = await client.get(
            f"http://{grafana.host}:{grafana.port}{ACTIVE_ALERTS_PATH}",
            params=ACTIVE_ONLY,
            timeout=READ_TIMEOUT,
        )
        response.raise_for_status()
        return count_firing(object_of(response.json))
    except (httpx.HTTPError, ValueError) as exc:
        _log.warning("alerts: Grafana active-alert read failed: %r", exc)
        return FiringAlerts(state="unavailable")
