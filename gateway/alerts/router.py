# meiva:note
# The gateway's alert surfaces. Grafana owns alert state, so nothing here stores it:
# `GET /api/alerts/firing` reads what Grafana reports firing right now (the top-bar badge),
# and `POST /api/alerts` is the webhook receiver of the alert pipeline's dead-man switch.
# meiva:link gateway/alerts/docs/alerts.md

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response

from base import telemetry
from gateway.alerts.agent_webhook import router as agent_webhook_router
from gateway.alerts.firing import WATCHDOG_METRIC, read_firing_alerts
from gateway.alerts.schemas import AlertWebhookPayload
from gateway.http.auth.webhook import authenticate_webhook
from gateway.http.config import gateway_clock, gateway_config, state_as
from gateway.routers.proxies.grafana import GrafanaAppState
from gateway.schemas.alerts import FiringAlerts

router = APIRouter()
router.include_router(agent_webhook_router)


@router.post("/api/alerts", status_code=204, response_class=Response)
# meiva:note
# The alert pipeline's heartbeat receiver: Grafana's contact point posts the standard
# Alertmanager webhook payload here, and a firing watchdog alert in it moves the heartbeat
# gauge the dead-man rule reads. Nothing else in the payload is kept or acted on — alert state
# stays in Grafana, and agents are told through `POST /api/alerts/agents/{agent_id}`.
def receive_alert_webhook(body: AlertWebhookPayload, request: Request) -> Response:
    if not authenticate_webhook(request, provider="alerts").authorized:
        raise HTTPException(status_code=401, detail="unauthorized webhook caller")

    if any(
        alert.labels.get("metric") == WATCHDOG_METRIC
        and (alert.status if "status" in alert.model_fields_set else body.status or alert.status)
        == "firing"
        for alert in body.alerts
    ):
        # meiva:note
        # One event per accepted watchdog POST. The gauge keeps moving while the webhook path
        # works and stops when it does not — a rotated token the gateway now rejects, a stopped
        # gateway, a contact point routed elsewhere — without depending on an alert that the
        # broken path would have had to deliver.
        telemetry.emit(
            "telemetry",
            "alerts",
            attributes={
                "last_ingest_timestamp_seconds": gateway_clock(request).now().timestamp(),
            },
        )
    return Response(status_code=204)


@router.get("/api/alerts/firing")
async def get_firing_alerts(request: Request) -> FiringAlerts:
    """Alerts Grafana reports firing right now — the top-bar badge.

    `state` says whether the counts can be trusted: a Grafana that does not answer is
    `unavailable` with no counts, never zero.
    """

    return await read_firing_alerts(
        state_as(request, GrafanaAppState).grafana_client, gateway_config(request).grafana
    )
