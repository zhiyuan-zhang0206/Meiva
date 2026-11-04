from dataclasses import replace

from langchain_openai import ChatOpenAI

from base.lm.provider_api import BuildContext, InferenceSpeed, ProviderKeys, ThinkingConfig
from base.lm.registry import ModelSpec
from meiva_builtins.plugins.lm_openai.provider import PROVIDER, build, served_speed


def _context(
    model: str,
    *,
    effort: str = "",
    thinking: ThinkingConfig | None = None,
    spec: ModelSpec | None = None,
) -> BuildContext:
    return BuildContext(
        model=model,
        spec=spec if spec is not None else PROVIDER.models[model],
        thinking=thinking,
        resolved_effort=effort,
        disable_streaming=False,
        timeout=None,
        thinking_budget_tokens=0,
        keys=ProviderKeys({"OPENAI_API_KEY": "test-not-a-real-key"}),
    )


def _built(context: BuildContext) -> ChatOpenAI:
    llm = build(context)
    assert isinstance(llm, ChatOpenAI)
    return llm


def test_the_default_asks_for_a_reasoning_summary_on_the_responses_api() -> None:
    llm = _built(_context("gpt-6-sol"))

    assert llm.use_responses_api is True
    assert llm.reasoning == {"effort": "medium", "summary": "auto"}
    assert llm.service_tier == "default"
    assert llm.model_name == "gpt-6-sol"


def test_an_explicit_effort_is_validated_and_sent() -> None:
    assert _built(_context("gpt-6-sol", effort="high")).reasoning == {
        "effort": "high",
        "summary": "auto",
    }


def test_disabled_thinking_drops_to_the_cheapest_effort_without_a_summary() -> None:
    assert _built(_context("gpt-6-sol", thinking={"type": "disabled"})).reasoning == {
        "effort": "none"
    }
    assert _built(_context("gpt-6-astra", thinking={"type": "disabled"})).reasoning == {
        "effort": "low"
    }


def test_a_fast_service_calls_the_standard_model_with_the_fast_tier() -> None:
    spec = replace(PROVIDER.models["gpt-6-sol"], fast_of="gpt-6-sol")
    llm = _built(_context("gpt-6-sol-fast", spec=spec))

    assert llm.model_name == "gpt-6-sol"
    assert llm.service_tier == "fast"


def test_served_speed_reads_the_service_tier_receipt() -> None:
    assert served_speed({"service_tier": "priority"}) is InferenceSpeed.FAST
    assert served_speed({"service_tier": "default"}) is InferenceSpeed.STANDARD
