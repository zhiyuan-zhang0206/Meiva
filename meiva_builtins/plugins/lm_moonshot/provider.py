"""Moonshot Kimi provider plugin for Meiva.

ChatMoonshot captures streamed reasoning in
``additional_kwargs["reasoning_content"]`` rather than canonical content
blocks. The streaming fan-out and timeline both consume that shape.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Annotation-only (`build` return): keeps the chat-model stack off the
    # provider-registration path (exec-child boot, task #3633;
    # `_TYPE_CHECKING_ALLOWED`).
    from langchain_core.language_models.chat_models import BaseChatModel
from loguru import logger

from base.api_contracts.agent_contract import Harness
from base.lm.compat.stop import StopSpec
from base.lm.effort import validate_effort
from base.lm.provider_api import (
    BuildContext,
    PriceRates,
    ProviderBinding,
    ProviderContribution,
)
from base.lm.registry import ModelSpec, ModelTuning
from base.packages.plugins.extensions import PluginContributions

_KIMI_EFFORT_LEVELS = ("low", "high", "max")


# meiva:note
# Moonshot Kimi (`https://api.moonshot.ai/v1`) streams reasoning in `delta.reasoning_content`,
# which `ChatMoonshot` records on `additional_kwargs["reasoning_content"]` for the fan-out and
# timeline readers. K3-series thinking (`kimi-k3`, `kimi-k3.1-pro*`) is always on and rejects
# the legacy K2.x `thinking` request field (`ChatMoonshot.thinking` is deliberately left unset),
# so `thinking={"type": "disabled"}` logs a warning while graded effort (`low`/`high`/`max`,
# default `max`) rides top-level `reasoning_effort` via `extra_body`.
# meiva:link base/lm/docs/lm/lm.meiva.okf.md
def build(ctx: BuildContext) -> BaseChatModel:
    from langchain_moonshot import ChatMoonshot

    api_key = ctx.keys.require("MOONSHOT_API_KEY")
    if ctx.thinking is not None and ctx.thinking.get("type") == "disabled":
        logger.warning(
            f"{ctx.model} cannot disable thinking; thinking={{'type': 'disabled'}} ignored"
        )
    kimi_kwargs: dict[str, Any] = {}
    if ctx.resolved_effort:
        kimi_kwargs["extra_body"] = {
            "reasoning_effort": validate_effort(
                ctx.resolved_effort,
                ctx.effort_levels if ctx.effort_levels is not None else _KIMI_EFFORT_LEVELS,
                target="kimi",
            )
        }
    return ChatMoonshot(
        model=ctx.model,  # type: ignore[call-arg]
        api_key=api_key,  # type: ignore[arg-type]
        stream_usage=True,
        disable_streaming=ctx.disable_streaming,
        timeout=ctx.timeout,
        **kimi_kwargs,
    )


PROVIDER = ProviderContribution(
    binding=ProviderBinding(
        prefix="kimi-",
        display_name="Moonshot",
        key_env="MOONSHOT_API_KEY",
        build=build,
        effort_levels=_KIMI_EFFORT_LEVELS,
        vision=True,
        stop_spec=StopSpec(
            "moonshot",
            "finish_reason",
            frozenset({"stop", "tool_calls", "function_call"}),
            frozenset({"length"}),
        ),
    ),
    models={
        # meiva:note
        # Display-only supersession (task #5609): `kimi-k3` stays spawnable for the configs
        # that already name it, while the picker offers Moonshot's post-trained successor.
        # meiva:link base/lm/docs/pricing-model-rates.meiva.okf.md
        "kimi-k3": ModelSpec(
            provider="kimi",
            spawnable=True,
            superseded_by="kimi-k3.1-pro",
            context_window=1_048_576,
            knowledge_cutoff="2025-12",
            model_identity="You are running on Kimi K3 (Moonshot).",
            effort_levels=("low", "high", "max"),
            thinking_always_on=True,
            # Streams (the registry default). The former streaming=False carried
            # "streaming returns ~40% 429" — no incident record backs the asymmetry,
            # and Moonshot's own troubleshooting page recommends stream=True
            # precisely to avoid connection errors (non-streaming makes the server
            # withhold the response header until generation completes). The
            # asymmetry is more likely an artifact of the timeouts this commit
            # fixes: the streaming path was killed by a 30s TTFT while the
            # non-streaming fallback got 600s. See
            # docs/decisions/engineering/design/simplification/2026-07-25-per-model-tuning-values.md.
            tuning=ModelTuning(
                # Pinned 2026-08-01 (task #568): Moonshot documents K3's default
                # effort as `max` (docs/decisions/2026-07-25-per-model-tuning-
                # values.md Decision 4).
                reasoning_effort="max",
                # Moonshot's rate limits are account-wide across every key AND every
                # model, capacity for K3 was scarce enough that new subscriptions
                # were paused, and engine_overloaded_error is explicitly defined as
                # server-side capacity that upgrading your tier does not fix.
                llm_retry_max_attempts=10,
                # The recorded K3 failure (PR #496): the provider SDK retries a 429
                # internally, gets a 200, but the overloaded engine never starts
                # streaming — no bytes, so 30s TTFT fired before the retry could land.
                llm_stream_ttft_timeout_seconds=120.0,
            ),
            media_types=frozenset({"image"}),
            external_harnesses=frozenset({Harness.KIMI_CODE}),
        ),
        # meiva:note
        # K3.1 Pro repeats K3's capability facts — the K3-series 1,048,576-token window,
        # always-on thinking on low/high/max with `max` as the default, and a 2025-12 cutoff
        # carried forward because Moonshot publishes none for this release. Highspeed is the
        # same model served 2-3x faster at twice the input/output rate, so it keeps its own id
        # and every fact instead of becoming a `fast_of` row, which derives `<id>-fast` and
        # needs a served-speed receipt.
        # meiva:link base/lm/docs/pricing-model-rates.meiva.okf.md
        "kimi-k3.1-pro": ModelSpec(
            provider="kimi",
            spawnable=True,
            context_window=1_048_576,
            knowledge_cutoff="2025-12",
            model_identity="You are running on Kimi K3.1 Pro (Moonshot).",
            effort_levels=("low", "high", "max"),
            thinking_always_on=True,
            tuning=ModelTuning(
                reasoning_effort="max",
                llm_retry_max_attempts=10,
                llm_stream_ttft_timeout_seconds=120.0,
            ),
            media_types=frozenset({"image"}),
            external_harnesses=frozenset({Harness.KIMI_CODE}),
        ),
        "kimi-k3.1-pro-highspeed": ModelSpec(
            provider="kimi",
            spawnable=True,
            context_window=1_048_576,
            knowledge_cutoff="2025-12",
            model_identity="You are running on Kimi K3.1 Pro (Moonshot).",
            effort_levels=("low", "high", "max"),
            thinking_always_on=True,
            tuning=ModelTuning(
                reasoning_effort="max",
                llm_retry_max_attempts=10,
                llm_stream_ttft_timeout_seconds=120.0,
            ),
            media_types=frozenset({"image"}),
            external_harnesses=frozenset({Harness.KIMI_CODE}),
        ),
    },
    pricing={
        "kimi-k3": PriceRates(
            cache_miss=3.0,
            cache_hit=0.30,
            output=15.0,
            cache_write_5m=3.0,
            cache_write_1h=6.0,
            source_url="https://platform.moonshot.ai/docs/pricing",
            source_checked_at="2026-10-24",
            vendor="moonshot",
        ),
        "kimi-k3.1-pro": PriceRates(
            cache_miss=2.0,
            cache_hit=0.20,
            output=10.0,
            cache_write_5m=2.0,
            cache_write_1h=4.0,
            source_url="https://platform.moonshot.ai/docs/pricing",
            source_checked_at="2026-10-24",
            vendor="moonshot",
        ),
        "kimi-k3.1-pro-highspeed": PriceRates(
            cache_miss=4.0,
            cache_hit=0.20,
            output=20.0,
            cache_write_5m=4.0,
            cache_write_1h=8.0,
            source_url="https://platform.moonshot.ai/docs/pricing",
            source_checked_at="2026-10-24",
            vendor="moonshot",
        ),
    },
)


def contribute() -> PluginContributions:
    """What this plugin declares: its model provider."""
    return PluginContributions(providers=(PROVIDER,))
