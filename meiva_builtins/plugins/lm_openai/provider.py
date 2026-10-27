"""OpenAI provider plugin for Meiva."""

# meiva:note
# The provider face: how a ``gpt-*`` call is built, and the contributions the plugin declares.
# The declarations' bulk — the models served, their price lattice and the independently priced
# Fast service — lives in sibling modules of this package, so a new OpenAI model adds rows there
# instead of growing one module past its budget.

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Annotation-only (`build` return): keeps the chat-model stack off the
    # provider-registration path (exec-child boot, task #3633;
    # `_TYPE_CHECKING_ALLOWED`).
    from langchain_core.language_models.chat_models import BaseChatModel

from base.lm.compat.stop import StopCategory, StopSpec
from base.lm.effort import validate_effort
from base.lm.provider_api import (
    BuildContext,
    DecisionBinding,
    DecisionBuildContext,
    DecisionModelSpec,
    DecisionProviderContribution,
    InferenceSpeed,
    PriceRates,
    ProviderBinding,
    ProviderContribution,
    with_fast_variants,
)
from base.packages.plugins.extensions import PluginContributions
from meiva_builtins.plugins.lm_openai.decision import DecisionsBinding
from meiva_builtins.plugins.lm_openai.fast_prices import FAST_PRICES, FAST_TPS
from meiva_builtins.plugins.lm_openai.models import EFFORT_LEVELS, MODELS
from meiva_builtins.plugins.lm_openai.prices import PRICES


class OpenAIServiceTier(StrEnum):
    """Service receipts supported for an explicitly requested Fast call."""

    DEFAULT = "default"
    FAST = "fast"
    PRIORITY = "priority"


def served_speed(metadata: Mapping[str, Any]) -> InferenceSpeed:
    """GPT-5.6 reports priority for Fast; ramp-rate downgrades report default."""
    tier = OpenAIServiceTier(metadata.get("service_tier"))
    return InferenceSpeed.STANDARD if tier == OpenAIServiceTier.DEFAULT else InferenceSpeed.FAST


def build(ctx: BuildContext) -> BaseChatModel:
    """gpt-* branch: ChatOpenAI on the Responses API. `reasoning.{effort,
    summary}` returns the reasoning summary as a canonical block; a
    caller disabling thinking drops to effort "none". (The Chat
    Completions path returns zero reasoning.)"""
    from langchain_openai import ChatOpenAI

    # Direct construction keeps this SDK-specific path explicit.
    api_key = ctx.keys.require("OPENAI_API_KEY")
    # The Chat Completions path returns zero reasoning. The Responses API
    # with `reasoning.{effort,summary}` returns the model's reasoning
    # summary as a `{"type":"reasoning","summary":[{"type":"summary_text",
    # "text":...}]}` content block, streamed incrementally — the streaming
    # fan-out and timeline pull the visible text out of summary[].text
    # (folded to the canonical `thinking` shape by base.lm.reasoning).
    #
    # effort must be set: the model's default reasoning effort is too low
    # to emit a summary, so `summary="auto"` alone yields zero reasoning
    # every turn (verified empirically). "medium" reliably surfaces a
    # summary; raw chain of thought stays provider-hidden, only the summary
    # is exposed. A caller disabling thinking (short-text paths) drops to
    # effort "none". Tool-call args still stream through `tool_call_chunks`,
    # so code rendering is unaffected by the API switch.
    thinking_disabled = ctx.thinking is not None and ctx.thinking.get("type") == "disabled"
    gpt_effort = "none" if thinking_disabled else (ctx.resolved_effort or "medium")
    model_levels = ctx.spec.effort_levels if ctx.spec is not None else None
    if thinking_disabled and model_levels is not None and "none" not in model_levels:
        # The binary thinking switch asks for the cheapest supported mode;
        # it is distinct from an explicit effort selection, which never remaps.
        gpt_effort = model_levels[0]
    # Validate the model's declared effort vocabulary (per-model
    # ModelSpec.effort_levels). GPT-6 Astra rejects "none" and "minimal";
    # GPT-6 Sol and Luna accept "none" but reject "minimal" (live-checked
    # 2026-09-25). An unsupported explicit effort raises. Models
    # without a declared vocabulary keep the historic verbatim passthrough.
    if model_levels is not None:
        gpt_effort = validate_effort(gpt_effort, model_levels, target=ctx.model)
    gpt_reasoning: dict[str, Any] = (
        {"effort": gpt_effort} if thinking_disabled else {"effort": gpt_effort, "summary": "auto"}
    )
    return ChatOpenAI(
        model=(ctx.spec.fast_of if ctx.spec and ctx.spec.fast_of else ctx.model),  # type: ignore[call-arg]
        api_key=api_key,  # type: ignore[arg-type]
        use_responses_api=True,
        reasoning=gpt_reasoning,
        service_tier="fast" if ctx.spec and ctx.spec.fast_of else "default",
        disable_streaming=ctx.disable_streaming,
        timeout=ctx.timeout,
    )


PROVIDER = ProviderContribution(
    binding=ProviderBinding(
        prefix="gpt-",
        display_name="OpenAI",
        key_env="OPENAI_API_KEY",
        build=build,
        effort_levels=EFFORT_LEVELS,
        vision=True,
        served_speed=served_speed,
        stop_spec=StopSpec(
            "openai",
            "finish_reason",
            frozenset({"stop", "tool_calls", "function_call"}),
            frozenset({"length"}),
            status_key="status",
            status_map={
                "completed": StopCategory.NORMAL,
                "incomplete": StopCategory.TRUNCATED,
            },
        ),
    ),
    models=MODELS,
    pricing=PRICES,
)


def build_decision(ctx: DecisionBuildContext) -> DecisionBinding:
    """The Decisions API client; fails at once when ``OPENAI_API_KEY`` is missing."""
    return DecisionsBinding(ctx, ctx.keys.require("OPENAI_API_KEY"))


# Decision ids are separate from chat ids: the same wire model (gpt-6-luna) is priced
# differently here (input only), so usage pricing and chat dispatch need distinct ids.
DECISION_PROVIDER = DecisionProviderContribution(
    prefix="gpt-",
    display_name="OpenAI",
    key_env="OPENAI_API_KEY",
    build=build_decision,
    models={
        # Text and images; ``decide()`` sends text only. The Decisions guide documents no
        # context window; the 272K input ceiling is the chat model's base price tier, kept
        # so a call never crosses into the long-context multiplier the guide mentions.
        "gpt-6-luna-decisions": DecisionModelSpec(
            context_window=272_000, media_types=frozenset({"image"})
        ),
    },
    pricing={
        # $0.10 per million input tokens; output, cache reads and cache writes are not charged,
        # so cached input is priced like any input.
        "gpt-6-luna-decisions": PriceRates(
            cache_miss=0.10,
            cache_hit=0.10,
            output=0.0,
            source_url="https://developers.openai.com/api/docs/guides/decisions",
            source_checked_at="2026-10-11",
            vendor="openai",
        ),
    },
)


def contribute() -> PluginContributions:
    """Standard and independently priced Fast inference services, and the Decisions API."""
    return PluginContributions(
        decision_providers=(DECISION_PROVIDER,),
        providers=(with_fast_variants(PROVIDER, FAST_PRICES, reference_tps=FAST_TPS),),
    )
