from __future__ import annotations

import asyncio
import time
from typing import Any, TypedDict, cast

from langchain_core.messages import AIMessage

from base.agents.messages.timestamp_policy import TimestampPolicy
from base.api_contracts.agent_contract import AgentStatus
from base.db import Database
from base.host.env.agent_slices import ModelOverrides
from base.lm.call import extract_text
from base.lm.catalog import ModelCatalog
from base.lm.catalog.decide.types import AsyncTransport, DecisionError, Question, YesNo
from base.lm.catalog.provider_keys import ProviderKeys
from base.lm.compat.stop import StopCategory, classify_stop
from base.lm.decision.call import decide
from base.lm.effort import ReasoningEffort
from base.lm.errors import ErrorClass, classify_error, normalize_provider_transport_error
from base.lm.factory import build_chat_model
from base.lm.usage import log_usage_from_message
from base.log import logger
from gateway.agents.history.timeline import load_current_timeline_items
from gateway.agents.reply_suggestion.cache import CACHE_ENTRIES, SuggestionCache
from gateway.agents.reply_suggestion.excerpt import ConversationExcerpt, build_excerpt
from gateway.agents.reply_suggestion.plan import (
    ReplySuggestionPolicy,
    draft_messages,
    draft_verdict,
    gate_refusal,
    model_problem,
    refusal_before_reading,
)
from gateway.agents.reply_suggestion.schemas import (
    DraftReplies,
    ReplySuggestionResponse,
    SuggestionReason,
)
from gateway.http.model_catalog import CatalogLoadFailure, require_catalog
from ops.agents import get_agent_status

GATE_USAGE_KIND = "reply_suggestion_gate"
DRAFT_USAGE_KIND = "reply_suggestion"


GATE_TIMEOUT_S = 15.0
DRAFT_TIMEOUT_S = 20.0

USEFUL_REPLY = "useful_reply"
GATE_QUESTION = YesNo(
    question="Does the agent's last turn leave the user something to answer or decide?",
    true_if="The agent asks the user to approve, choose, confirm or supply something.",
    false_if=(
        "The agent only reports progress, waits on another agent or a running job, or has finished."
    ),
)


class ReplySuggestionConfigurationError(ValueError):
    pass


# meiva:note
# How the drafting model is built: reasoning off (DeepSeek defaults to maximum effort,
# which makes a one-line draft run for tens of seconds), a bounded wait, and no metering of
# its own (the service logs the usage).
def draft_model_options(llm_override: str) -> dict[str, Any]:
    return {
        "reasoning_effort": ReasoningEffort.NONE,
        "timeout": DRAFT_TIMEOUT_S,
        "meter": False,
        "llm_override": llm_override,
        "overrides": ModelOverrides.from_pins({}),
    }


# meiva:note
# What a structured-output call returns with `include_raw=True`: the provider's own message, the
# parsed value (None when parsing failed) and the parsing error, if any.
class _StructuredReply(TypedDict):
    raw: AIMessage
    parsed: object
    parsing_error: BaseException | None


def _structured_reply(reply: object) -> _StructuredReply:
    if not isinstance(reply, dict) or not {"raw", "parsed", "parsing_error"} <= reply.keys():
        raise TypeError("a structured call with include_raw=True returns raw/parsed/error")
    return cast(_StructuredReply, reply)


# meiva:note
# Gate, draft and remember reply suggestions.
#
# The gate asks its question through `decide` over `transport` (None dials the provider; a test
# hands a transport that answers the provider's wire), and the drafting chat model is built
# from the catalog like any other, with the cluster's `llm_override`. Everything else a request
# needs (the store, the settings, the catalog) arrives with the request.
class ReplySuggestionService:
    def __init__(self, *, llm_override: str, transport: AsyncTransport | None) -> None:
        self._llm_override = llm_override
        self._transport = transport
        self._cache = SuggestionCache(CACHE_ENTRIES)

    async def suggest(
        self,
        agent_id: int,
        *,
        db: Database,
        policy: ReplySuggestionPolicy,
        rendering: TimestampPolicy,
        catalog: ModelCatalog | CatalogLoadFailure,
        keys: ProviderKeys | None,
    ) -> ReplySuggestionResponse:
        status = await asyncio.to_thread(get_agent_status, db, agent_id)
        refusal = refusal_before_reading(enabled=policy.enabled, idle=status is AgentStatus.IDLING)
        if refusal is not None:
            return _none(refusal)
        items = await asyncio.to_thread(load_current_timeline_items, db, agent_id, inputs=rendering)
        excerpt = build_excerpt(items, policy.max_input_tokens)
        if excerpt is None:
            return _none("no_agent_text")
        ready = require_catalog(catalog)
        if keys is None:
            raise RuntimeError("the provider keys are read whenever the catalog loaded")
        problem = model_problem(policy, ready.models, ready.decision_models)
        if problem is not None:
            raise ReplySuggestionConfigurationError(problem)
        key = (agent_id, excerpt.last_message_id)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        result = await self._gate_and_draft(agent_id, excerpt, policy, ready, keys)
        if result.reason in (None, "not_useful"):
            self._cache.remember(key, result)
        return result

    async def _gate_and_draft(
        self,
        agent_id: int,
        excerpt: ConversationExcerpt,
        policy: ReplySuggestionPolicy,
        catalog: ModelCatalog,
        keys: ProviderKeys,
    ) -> ReplySuggestionResponse:
        try:
            useful = await self._useful_reply_probability(agent_id, excerpt, policy, catalog, keys)
        except DecisionError:
            logger.opt(exception=True).warning(
                "reply suggestion gate failed for agent {agent_id}",
                event="reply_suggestion_gate_failed",
                agent_id=agent_id,
            )
            return _none("gate_failed")
        logger.info(
            "reply suggestion gate for agent {agent_id}: useful={useful:.3f} threshold={threshold}",
            event="reply_suggestion_gate",
            agent_id=agent_id,
            useful=useful,
            threshold=policy.gate_threshold,
        )
        refusal = gate_refusal(useful, policy.gate_threshold)
        if refusal is not None:
            return _none(refusal)
        return await self._draft(agent_id, excerpt, policy, catalog, keys)

    async def _useful_reply_probability(
        self,
        agent_id: int,
        excerpt: ConversationExcerpt,
        policy: ReplySuggestionPolicy,
        catalog: ModelCatalog,
        keys: ProviderKeys,
    ) -> float:
        questions: dict[str, Question] = {USEFUL_REPLY: GATE_QUESTION}
        decisions = await decide(
            policy.gate_model,
            excerpt.text,
            questions,
            catalog=catalog,
            keys=keys,
            usage_kind=GATE_USAGE_KIND,
            for_agent_id=agent_id,
            timeout=GATE_TIMEOUT_S,
            transport=self._transport,
        )
        return decisions[USEFUL_REPLY].probability("true")

    async def _draft(
        self,
        agent_id: int,
        excerpt: ConversationExcerpt,
        policy: ReplySuggestionPolicy,
        catalog: ModelCatalog,
        keys: ProviderKeys,
    ) -> ReplySuggestionResponse:
        chat = build_chat_model(
            policy.model, catalog=catalog, keys=keys, **draft_model_options(self._llm_override)
        )
        llm = chat.with_structured_output(
            DraftReplies.model_json_schema(), method="json_schema", strict=True, include_raw=True
        )
        started = time.monotonic()
        try:
            response = _structured_reply(
                await llm.ainvoke(draft_messages(policy.max_candidates, excerpt.text))
            )
        except Exception as exc:
            failure = normalize_provider_transport_error(exc)
            if classify_error(failure).error_class is ErrorClass.UNKNOWN:
                raise
            logger.opt(exception=True).warning(
                "reply suggestion draft failed for agent {agent_id}",
                event="reply_suggestion_draft_failed",
                agent_id=agent_id,
            )
            return _none("generation_failed")
        log_usage_from_message(
            response["raw"],
            policy.model,
            usage_kind=DRAFT_USAGE_KIND,
            latency_ms=(time.monotonic() - started) * 1000,
            for_agent_id=agent_id,
            catalog=catalog,
        )
        stop, _ = classify_stop(response["raw"], stops=catalog.stops)
        verdict = draft_verdict(
            finished=stop is StopCategory.NORMAL,
            parse_failed=response["parsing_error"] is not None,
            text=extract_text(response["raw"]),
            parsed=response["parsed"],
            max_candidates=policy.max_candidates,
        )
        if verdict.rejection is not None:
            logger.warning(
                "reply suggestion draft for agent {agent_id} rejected: {rejection}",
                event="reply_suggestion_draft_rejected",
                agent_id=agent_id,
                rejection=verdict.rejection,
                stop=stop.value,
                max_candidates=policy.max_candidates,
            )
            return _none("invalid_generation")
        return ReplySuggestionResponse(suggestions=verdict.suggestions)


def _none(reason: SuggestionReason) -> ReplySuggestionResponse:
    return ReplySuggestionResponse(suggestions=[], reason=reason)
