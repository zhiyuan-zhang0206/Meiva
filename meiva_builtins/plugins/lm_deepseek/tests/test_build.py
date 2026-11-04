import pytest
from pydantic import BaseModel

from base.lm.provider_api import BuildContext, ProviderKeys, ThinkingConfig
from meiva_builtins.plugins.lm_deepseek.chat import DeepSeekChatModel
from meiva_builtins.plugins.lm_deepseek.provider import PROVIDER, build

_MODEL = "deepseek-flash"


class _Answer(BaseModel):
    text: str


def _context(effort: str, thinking: ThinkingConfig | None = None) -> BuildContext:
    return BuildContext(
        model=_MODEL,
        spec=PROVIDER.models[_MODEL],
        thinking=thinking,
        resolved_effort=effort,
        disable_streaming=False,
        timeout=None,
        thinking_budget_tokens=0,
        keys=ProviderKeys({"DEEPSEEK_API_KEY": "test-not-a-real-key"}),
    )


def _built(context: BuildContext) -> DeepSeekChatModel:
    llm = build(context)
    assert isinstance(llm, DeepSeekChatModel)
    return llm


@pytest.mark.parametrize("effort", ["high", "max"])
def test_effort_rides_the_anthropic_output_config(effort: str) -> None:
    llm = _built(_context(effort))

    assert llm.model_kwargs == {"extra_body": {"output_config": {"effort": effort}}}
    assert llm.thinking is None


def test_effort_none_turns_thinking_off() -> None:
    llm = _built(_context("none"))

    assert llm.model_kwargs == {}
    assert llm.thinking == {"type": "disabled"}


def test_a_disabled_thinking_request_sends_no_effort() -> None:
    llm = _built(_context("high", thinking={"type": "disabled"}))

    assert llm.model_kwargs == {}
    assert llm.thinking == {"type": "disabled"}


def test_json_schema_output_uses_the_responses_endpoint_with_the_stored_effort() -> None:
    llm = _built(_context("max"))

    runnable = llm.with_structured_output(_Answer, method="json_schema")

    assert runnable is not None
