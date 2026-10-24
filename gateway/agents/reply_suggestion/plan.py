# meiva:note
# What a reply suggestion decides, as functions of values.
#
# `service.py` performs the reads and the two model calls; every judgment between them is here:
# whether to look at the conversation at all, whether the configured models fit their roles,
# whether the gate lets a draft through, and whether a draft is a usable answer.

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import ValidationError

from gateway.agents.reply_suggestion.schemas import DraftReplies, SuggestionReason

__all__ = [
    "DraftVerdict",
    "ReplySuggestionPolicy",
    "draft_instruction",
    "draft_messages",
    "draft_verdict",
    "gate_refusal",
    "model_problem",
    "refusal_before_reading",
]


# meiva:note
# The suggestion settings one request reads.
@dataclass(frozen=True)
class ReplySuggestionPolicy:
    enabled: bool
    model: str
    gate_model: str
    gate_threshold: float
    max_input_tokens: int
    max_candidates: int


# meiva:note
# A draft that is usable (`suggestions`) or the reason it is not (`rejection`).
#
# `unfinished`: the provider did not finish normally or the structured output did not parse.
# `invalid`: the text or the candidates break the contract. `too_many`: more candidates than
# the configured bound.
@dataclass(frozen=True)
class DraftVerdict:
    suggestions: list[str]
    rejection: Literal["unfinished", "invalid", "too_many"] | None


# meiva:note
# The two messages of a draft call: the instruction first, then the conversation excerpt.
def draft_messages(max_candidates: int, excerpt_text: str) -> list[BaseMessage]:
    return [
        SystemMessage(content=draft_instruction(max_candidates)),
        HumanMessage(content=excerpt_text),
    ]


def draft_instruction(max_candidates: int) -> str:
    return (
        "Draft replies the user could send next, in the user's language. "
        "Give one reply when the user's next move is obvious; give more only when the "
        f"conversation really offers a choice, and never more than {max_candidates}. "
        "When the agent asks the user to approve or confirm going ahead, the first reply is "
        "the shortest natural answer the user would send ('go' or 'not yet'), not a sentence."
    )


# meiva:note
# Why the conversation is not even read: the feature is off, or the agent is mid-turn.
def refusal_before_reading(*, enabled: bool, idle: bool) -> SuggestionReason | None:
    if not enabled:
        return "disabled"
    if not idle:
        return "agent_not_idle"
    return None


# meiva:note
# What is wrong with the configured models, or None when each fits its role.
#
# A disabled feature checks nothing.
def model_problem(
    policy: ReplySuggestionPolicy, chat_models: Collection[str], decision_models: Collection[str]
) -> str | None:
    if not policy.enabled:
        return None
    if policy.model not in chat_models:
        return (
            f"reply_suggestion_model {policy.model!r} is not a registered chat model; "
            f"available: {sorted(chat_models)}"
        )
    if policy.gate_model not in decision_models:
        return (
            f"reply_suggestion_gate_model {policy.gate_model!r} is not a registered decision "
            f"model; available: {sorted(decision_models)}"
        )
    return None


# meiva:note
# `not_useful` when the gate's probability is below the threshold; at it, the draft goes on.
def gate_refusal(useful: float, threshold: float) -> SuggestionReason | None:
    return "not_useful" if useful < threshold else None


# meiva:note
# Judge a structured draft: it finished normally, parsed, is valid JSON of valid candidates,
# and holds no more candidates than the bound (which is a contract, never a truncation).
def draft_verdict(
    *, finished: bool, parse_failed: bool, text: str, parsed: object, max_candidates: int
) -> DraftVerdict:
    if not finished or parse_failed:
        return DraftVerdict([], "unfinished")
    try:
        json.loads(text)
        suggestions = DraftReplies.model_validate(parsed).suggestions
    except (json.JSONDecodeError, ValidationError):
        return DraftVerdict([], "invalid")
    if len(suggestions) > max_candidates:
        return DraftVerdict([], "too_many")
    return DraftVerdict(suggestions, None)
