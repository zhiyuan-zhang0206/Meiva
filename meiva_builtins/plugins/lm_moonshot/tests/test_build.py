import pytest
from langchain_moonshot import ChatMoonshot

from base.lm.provider_api import BuildContext, ProviderKeys
from meiva_builtins.plugins.lm_moonshot.provider import PROVIDER, build


def _context(model: str, effort: str) -> BuildContext:
    return BuildContext(
        model=model,
        spec=PROVIDER.models[model],
        thinking=None,
        resolved_effort=effort,
        disable_streaming=False,
        timeout=None,
        thinking_budget_tokens=0,
        keys=ProviderKeys({"MOONSHOT_API_KEY": "test-not-a-real-key"}),
    )


@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_graded_effort_rides_top_level_reasoning_effort(effort: str) -> None:
    llm = build(_context("kimi-k3.1-pro", effort))

    assert isinstance(llm, ChatMoonshot)
    assert llm.extra_body == {"reasoning_effort": effort}


def test_no_resolved_effort_sends_no_extra_body() -> None:
    llm = build(_context("kimi-k3.1-pro", ""))

    assert isinstance(llm, ChatMoonshot)
    assert llm.extra_body is None
