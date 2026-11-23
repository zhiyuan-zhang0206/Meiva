import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request, Response

from base.agents.messages.inbound_provenance import InboundProvenance
from base.api_contracts.contracts import ALERT_AGENT_WEBHOOK_PATH
from gateway.alerts.notification_text import notification_text
from gateway.alerts.schemas import AlertWebhookPayload
from gateway.http.auth.webhook import authenticate_webhook
from gateway.http.config import gateway_clock, gateway_rpc, gateway_state
from ops.agents import get_agent_status
from ops.lifecycle.delivery import deliver_chat_inbound

router = APIRouter()


@router.post(
    ALERT_AGENT_WEBHOOK_PATH, status_code=204, response_class=Response, include_in_schema=False
)
async def post_agent_alert(
    agent_id: Annotated[int, Path(ge=1, le=2**63 - 1)],
    body: AlertWebhookPayload,
    request: Request,
) -> Response:
    auth = authenticate_webhook(request, provider="alerts")
    if not auth.authorized:
        raise HTTPException(status_code=401, detail="unauthorized webhook caller")
    if not body.alerts:
        raise HTTPException(status_code=422, detail="notification must contain at least one alert")

    state = gateway_state(request)
    await asyncio.to_thread(get_agent_status, state.db, agent_id)
    content = notification_text(body, now=gateway_clock(request).now())
    await deliver_chat_inbound(
        state.db_pool,
        state.db,
        state.bus,
        agent_id,
        prepare=lambda _conn: content,
        source="system:alertmanager",
        provenance=InboundProvenance(auth.source_verified_by, "alertmanager_webhook"),
        local_machine=state.machine.name(),
        rpc=gateway_rpc(request),
    )
    return Response(status_code=204)
