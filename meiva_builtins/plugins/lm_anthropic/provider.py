"""Anthropic Claude provider plugin for Meiva."""

# meiva:note
# The provider face: how a ``claude-*`` call is built, and the contributions the plugin
# declares. The declarations themselves — the models served and their price lattice — live in
# sibling modules of this package, so a new Claude model adds rows there instead of growing one
# module past its budget.

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Annotation-only (`build` return): keeps the chat-model stack off the
    # provider-registration path (exec-child boot, task #3633;
    # `_TYPE_CHECKING_ALLOWED`).
    from langchain_core.language_models.chat_models import BaseChatModel
from loguru import logger

from base.lm.compat.stop import StopSpec
from base.lm.effort import validate_effort
from base.lm.provider_api import (
    AttachPolicy,
    BuildContext,
    InferenceSpeed,
    PricePeriod,
    PriceRates,
    PriceTier,
    ProviderBinding,
    ProviderContribution,
    with_fast_variants,
)
from base.lm.registry import ModelSpec
from base.packages.plugins.extensions import PluginContributions
from meiva_builtins.plugins.lm_anthropic.models import MODELS
from meiva_builtins.plugins.lm_anthropic.prices import PRICES

# Budget used when MEIVA_REASONING_EFFORT enables an extended-thinking-only Claude
# model to its "on" tier and no explicit MEIVA_CLAUDE_THINKING_BUDGET_TOKENS is
# configured (0 = unset). Anthropic's minimum is 1024; this sits well under
# haiku-4-5's 64K max_tokens cap with headroom left for the actual response.
_CLAUDE_EXTENDED_THINKING_DEFAULT_BUDGET = 8192


def served_speed(metadata: Mapping[str, Any]) -> InferenceSpeed:
    """Parse the actual speed retained from Anthropic usage by the adapter."""
    return InferenceSpeed(metadata.get("speed"))


def claude_extended_thinking_kwarg(
    model: str,
    *,
    thinking: Mapping[str, Any] | None,
    budget_tokens: int,
    reasoning_effort: str,
    spec: ModelSpec | None,
) -> dict[str, Any] | None:
    """Resolve the `thinking` kwarg for extended-thinking-only claude models
    (`ModelSpec.extended_thinking_only`, currently haiku-4-5) when the caller
    passed none explicitly.

    `budget_tokens` (the resolved claude_thinking_budget_tokens, an explicit
    numeric budget) wins when set; otherwise the exact `reasoning_effort`
    value "high" from the model's binary `effort_levels` opts in at
    `_CLAUDE_EXTENDED_THINKING_DEFAULT_BUDGET` — these models have no `effort`
    wire field, so this is the knob's only effect on them. None = leave
    thinking unset (provider default OFF); also None for any model that is not
    extended-thinking-only or when the caller already set `thinking`.
    """
    if thinking is not None or spec is None or not spec.extended_thinking_only:
        return None
    if budget_tokens > 0:
        return {"type": "enabled", "budget_tokens": budget_tokens}
    if reasoning_effort:
        levels = spec.effort_levels
        if levels is not None and validate_effort(reasoning_effort, levels, target=model) != "none":
            return {"type": "enabled", "budget_tokens": _CLAUDE_EXTENDED_THINKING_DEFAULT_BUDGET}
    return None


def _effective_thinking(ctx: BuildContext, spec: ModelSpec) -> Mapping[str, Any] | None:
    thinking = ctx.thinking
    if thinking is not None and thinking.get("type") == "disabled" and spec.thinking_always_on:
        logger.warning(
            f"{ctx.model} cannot disable thinking; thinking={{'type': 'disabled'}} ignored"
        )
        return None
    return thinking


def _service_kwargs(spec: ModelSpec) -> dict[str, Any]:
    """Declare caching and opt into the provider's Fast service when selected."""
    model_kwargs: dict[str, Any] = {"cache_control": {"type": "ephemeral"}}
    kwargs: dict[str, Any] = {"model_kwargs": model_kwargs}
    if spec.fast_of is not None:
        model_kwargs["speed"] = InferenceSpeed.FAST.value
        kwargs["betas"] = ["fast-mode-2026-02-01"]
    return kwargs


def _effort_kwargs(ctx: BuildContext, spec: ModelSpec, *, disabled: bool) -> dict[str, Any]:
    """Adaptive effort uses the model's exact vocabulary; binary thinking is separate."""
    effort = ctx.resolved_effort
    if not effort or disabled or spec.extended_thinking_only:
        return {}
    if spec.effort_levels is not None:
        return {"effort": validate_effort(effort, spec.effort_levels, target=ctx.model)}
    logger.warning(
        f"{ctx.model} does not support reasoning effort; MEIVA_REASONING_EFFORT={effort!r} ignored"
    )
    return {}


def build(ctx: BuildContext) -> BaseChatModel:
    """claude-* branch: ThinkingTokensChatAnthropic, pinned max_tokens.

    Extended-thinking-only models (haiku-4-5) map budget_tokens / effort
    onto their thinking on/off binary (`claude_extended_thinking_kwarg`);
    adaptive-thinking models get `display: "summarized"` forced on so the
    wire returns thinking text the timeline can render. Reasoning effort
    rides the `effort` field, gated per model.
    """
    from base.lm.compat.anthropic_thinking import ThinkingTokensChatAnthropic

    # Fail fast on missing key — the same posture as every other provider
    # branch. Without it, ChatAnthropic reads ANTHROPIC_API_KEY from env
    # (or not) and hangs rather than surfacing a clear error.
    api_key = ctx.keys.require("ANTHROPIC_API_KEY")

    # Per-model output cap (ModelSpec.max_output_tokens). Same fail-fast
    # posture as the deepseek branch — an unregistered claude model raises
    # rather than falling back to langchain's stale profile table (4096
    # for unknown ids).
    spec = ctx.spec
    if spec is None or spec.max_output_tokens is None:
        raise ValueError(
            f"Unknown claude model {ctx.model!r} — register it (with "
            f"max_output_tokens) in this plugin's ModelSpec rows"
        )

    thinking = _effective_thinking(ctx, spec)
    thinking_disabled = thinking is not None and thinking.get("type") == "disabled"
    extra_kwargs = _service_kwargs(spec)
    if thinking is not None:
        extra_kwargs["thinking"] = thinking
    if ctx.disable_streaming:
        extra_kwargs["disable_streaming"] = True

    # Reasoning effort rides ChatAnthropic's `effort` field
    # (output_config.effort on the wire), gated per model — extended-
    # thinking-only models (haiku-4-5) have no effort support (server 400)
    # and map the knob onto their thinking on/off binary below instead.
    # Caller-disabled thinking skips effort injection, mirroring deepseek:
    # an explicit cheap/fast path shouldn't have the global env push
    # reasoning back in.
    effort = ctx.resolved_effort
    claude_kwargs = _effort_kwargs(ctx, spec, disabled=thinking_disabled)

    # Extended-thinking-only models (haiku-4-5) map budget_tokens / effort
    # onto their thinking on/off binary.
    extended_thinking = claude_extended_thinking_kwarg(
        ctx.model,
        thinking=thinking,
        budget_tokens=ctx.thinking_budget_tokens,
        spec=spec,
        reasoning_effort=effort,
    )
    if extended_thinking is not None:
        extra_kwargs["thinking"] = extended_thinking
    elif not thinking_disabled and not spec.extended_thinking_only:
        # Adaptive-thinking claude models (everything on the current API
        # except the extended-thinking-only ones) default to
        # `thinking.display="omitted"` server-side: the model thinks, but
        # the wire returns only a signature with no thinking text — the
        # stream emits no thinking_delta and the committed message carries
        # an empty thinking block, so the timeline has nothing to render.
        # Opt into summarized thinking text explicitly; a caller-passed
        # config keeps its own `display` when it set one.
        wire_thinking: dict[str, Any] = (
            dict(thinking) if thinking is not None else {"type": "adaptive"}
        )
        wire_thinking.setdefault("display", "summarized")
        extra_kwargs["thinking"] = wire_thinking

    # ThinkingTokensChatAnthropic subclasses ChatAnthropic to also surface
    # thinking_tokens in usage_metadata.output_token_details (the base
    # _create_usage_metadata drops them). Same __init__ signature.
    # cache_control enables Anthropic prompt caching — the system prompt and
    # eligible message blocks get cached server-side for 5 minutes (default TTL),
    # reducing input token cost and latency on repeated turns.
    return ThinkingTokensChatAnthropic(
        model=spec.fast_of or ctx.model,  # type: ignore[call-arg]
        api_key=api_key,
        max_tokens=spec.max_output_tokens,  # type: ignore[call-arg]
        timeout=ctx.timeout,
        **claude_kwargs,
        **extra_kwargs,
    )


PROVIDER = ProviderContribution(
    binding=ProviderBinding(
        prefix="claude-",
        display_name="Anthropic",
        key_env="ANTHROPIC_API_KEY",
        build=build,
        effort_levels=None,
        vision=True,
        anthropic_protocol=True,
        served_speed=served_speed,
        attach=AttachPolicy(
            file_size_limits={"image": 10 * 1024 * 1024, "pdf": 32 * 1024 * 1024},
            image_dimension_tiers=((1, 8000),),
            pdf_document_block=True,
        ),
        stop_spec=StopSpec(
            "anthropic",
            "stop_reason",
            frozenset({"end_turn", "tool_use", "refusal"}),
            frozenset({"max_tokens"}),
        ),
    ),
    models=MODELS,
    pricing=PRICES,
)


def contribute() -> PluginContributions:
    """Standard and independently priced Fast inference services."""
    return PluginContributions(
        providers=(
            with_fast_variants(
                PROVIDER,
                {
                    "claude-opus-5": PriceRates(
                        cache_miss=10,
                        cache_hit=1,
                        output=50,
                        cache_write_5m=12.50,
                        cache_write_1h=20,
                        source_url="https://platform.claude.com/docs/en/about-claude/pricing",
                        source_checked_at="2026-10-08",
                        vendor="anthropic",
                        periods=(
                            PricePeriod(
                                effective_from=None,
                                effective_until=None,
                                tiers=(
                                    PriceTier(
                                        input_tokens_min=0,
                                        input_tokens_max=None,
                                        cache_miss="10",
                                        cache_hit="1",
                                        output="50",
                                        cache_write_5m="12.50",
                                        cache_write_1h="20",
                                    ),
                                ),
                            ),
                        ),
                    ),
                    "claude-opus-5-5": PriceRates(
                        cache_miss=8,
                        cache_hit=0.4,
                        output=40,
                        cache_write_5m=10.00,
                        cache_write_1h=16,
                        source_url="https://platform.claude.com/docs/en/about-claude/pricing",
                        source_checked_at="2026-10-08",
                        vendor="anthropic",
                        periods=(
                            PricePeriod(
                                effective_from=None,
                                effective_until=None,
                                tiers=(
                                    PriceTier(
                                        input_tokens_min=0,
                                        input_tokens_max=None,
                                        cache_miss="8",
                                        cache_hit="0.4",
                                        output="40",
                                        cache_write_5m="10.00",
                                        cache_write_1h="16",
                                    ),
                                ),
                            ),
                        ),
                    ),
                },
            ),
        )
    )
