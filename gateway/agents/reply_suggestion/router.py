from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from base.lm.catalog import ModelCatalog
from base.lm.factory import process_provider_keys
from gateway.agents.eval_guard import deny_isolated_result_read
from gateway.agents.history.configuration import timeline_rendering
from gateway.agents.reply_suggestion.plan import ReplySuggestionPolicy
from gateway.agents.reply_suggestion.schemas import ReplySuggestionResponse
from gateway.agents.reply_suggestion.service import (
    ReplySuggestionConfigurationError,
    ReplySuggestionService,
)
from gateway.http.config import (
    GatewayAppState,
    gateway_clock,
    gateway_config,
    state_as,
)
from gateway.http.disconnect import run_until_disconnect
from gateway.http.middleware.error_envelope import error_response
from gateway.http.model_catalog import request_catalog

router = APIRouter()
_log = logging.getLogger(__name__)


# meiva:note
# The suggestion service and policy the app root builds once; this directory's route is their only
# reader, so the directory declares them.
class ReplySuggestionAppState(GatewayAppState):
    reply_suggestions: ReplySuggestionService
    reply_suggestion_policy: ReplySuggestionPolicy


@router.post(
    "/api/agents/{agent_id}/reply-suggestion",
    dependencies=[Depends(deny_isolated_result_read)],
    response_model=ReplySuggestionResponse,
    description="Suggest replies when helping the user respond would be useful.\n\n200 with an empty `suggestions` array and a `reason` when there is nothing to show: the\nagent is mid-turn, a reply suggestion is not useful, or the gate or draft\nmodel failed (logged as a warning). 404 for an unknown agent; 503 when the conversation\ncannot be read. The work stops when the client disconnects.",
)
async def post_reply_suggestion(
    agent_id: int, request: Request
) -> ReplySuggestionResponse | JSONResponse:

    suggestion_state = state_as(request, ReplySuggestionAppState)
    catalog = request_catalog(request)
    keys = (
        await asyncio.to_thread(process_provider_keys, catalog)
        if isinstance(catalog, ModelCatalog)
        else None
    )
    try:
        result = await run_until_disconnect(
            suggestion_state.reply_suggestions.suggest(
                agent_id,
                db=suggestion_state.db,
                policy=suggestion_state.reply_suggestion_policy,
                rendering=timeline_rendering(gateway_config(request), gateway_clock(request)),
                catalog=catalog,
                keys=keys,
            ),
            request.is_disconnected,
        )
    except ReplySuggestionConfigurationError:
        _log.exception("Reply suggestion model configuration is invalid")
        return error_response(
            request,
            code="reply_suggestion_unavailable",
            status=503,
            detail="Reply suggestion models are not configured correctly.",
            retryable=False,
        )

    return (
        result
        if result is not None
        else ReplySuggestionResponse(suggestions=[], reason="client_disconnected")
    )
