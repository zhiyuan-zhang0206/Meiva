from base.lm.compat.openai_reasoning import ReasoningContentChatModel
from base.lm.provider_api import BuildContext, ProviderKeys, ThinkingConfig
from meiva_builtins.plugins.lm_zhipu.provider import PROVIDER, build


def _context(model: str, effort: str, thinking: ThinkingConfig | None = None) -> BuildContext:
    return BuildContext(
        model=model,
        spec=PROVIDER.models[model],
        thinking=thinking,
        resolved_effort=effort,
        disable_streaming=False,
        timeout=None,
        thinking_budget_tokens=0,
        keys=ProviderKeys({"GLM_API_KEY": "test-not-a-real-key"}),
    )


def test_disabled_thinking_rides_extra_body_and_skips_effort() -> None:
    llm = build(_context("glm-5.2", "high", thinking={"type": "disabled"}))

    assert isinstance(llm, ReasoningContentChatModel)
    assert llm.extra_body == {"thinking": {"type": "disabled"}}
    assert llm.reasoning_effort is None


def test_effort_rides_the_reasoning_effort_field() -> None:
    llm = build(_context("glm-5.2", "low"))

    assert isinstance(llm, ReasoningContentChatModel)
    assert llm.extra_body is None
    assert llm.reasoning_effort == "low"


def test_always_on_thinking_ignores_a_disable_request() -> None:
    llm = build(_context("glm-5.3", "", thinking={"type": "disabled"}))

    assert isinstance(llm, ReasoningContentChatModel)
    assert llm.extra_body is None
    assert llm.reasoning_effort is None
