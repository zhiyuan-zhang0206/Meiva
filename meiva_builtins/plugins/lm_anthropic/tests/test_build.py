from dataclasses import replace

import pytest

from base.lm.compat.anthropic_thinking import ThinkingTokensChatAnthropic
from base.lm.provider_api import BuildContext, ProviderKeys, ThinkingConfig
from base.lm.registry import ModelSpec
from meiva_builtins.plugins.lm_anthropic.provider import PROVIDER, build


def _context(
    model: str,
    *,
    effort: str = "",
    thinking: ThinkingConfig | None = None,
    budget: int = 0,
    spec: ModelSpec | None = None,
    disable_streaming: bool = False,
) -> BuildContext:
    return BuildContext(
        model=model,
        spec=spec if spec is not None else PROVIDER.models[model],
        thinking=thinking,
        resolved_effort=effort,
        disable_streaming=disable_streaming,
        timeout=None,
        thinking_budget_tokens=budget,
        keys=ProviderKeys({"ANTHROPIC_API_KEY": "test-not-a-real-key"}),
    )


def _built(context: BuildContext) -> ThinkingTokensChatAnthropic:
    llm = build(context)
    assert isinstance(llm, ThinkingTokensChatAnthropic)
    return llm


def test_an_adaptive_model_asks_for_summarized_thinking_and_caches() -> None:
    llm = _built(_context("claude-sonnet-5"))

    assert llm.thinking == {"type": "adaptive", "display": "summarized"}
    assert llm.model_kwargs == {"cache_control": {"type": "ephemeral"}}
    assert llm.effort is None
    assert llm.betas is None


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_effort_rides_the_effort_field(effort: str) -> None:
    llm = _built(_context("claude-sonnet-5", effort=effort))

    assert llm.effort == effort


def test_disabled_thinking_skips_effort_and_keeps_the_disable() -> None:
    llm = _built(_context("claude-sonnet-5", effort="high", thinking={"type": "disabled"}))

    assert llm.thinking == {"type": "disabled"}
    assert llm.effort is None


def test_an_extended_thinking_only_model_maps_its_budget_onto_thinking() -> None:
    llm = _built(_context("claude-haiku-4-5-20251001", budget=2048))

    assert llm.thinking == {"type": "enabled", "budget_tokens": 2048}
    assert llm.effort is None


def test_a_fast_service_declares_its_beta_and_speed() -> None:
    spec = replace(PROVIDER.models["claude-opus-5"], fast_of="claude-opus-5")
    llm = _built(_context("claude-opus-5-fast", spec=spec))

    assert llm.betas == ["fast-mode-2026-02-01"]
    assert llm.model_kwargs == {"cache_control": {"type": "ephemeral"}, "speed": "fast"}


def test_streaming_can_be_disabled() -> None:
    assert _built(_context("claude-sonnet-5", disable_streaming=True)).disable_streaming is True
