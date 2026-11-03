from base.lm.compat.openai_reasoning import ReasoningContentChatModel
from base.lm.provider_api import BuildContext, ProviderKeys, ThinkingConfig
from meiva_builtins.plugins.lm_xiaomi.provider import PROVIDER, build


def _context(effort: str, thinking: ThinkingConfig | None = None) -> BuildContext:
    model = "mimo-v2.6-pro"
    return BuildContext(
        model=model,
        spec=PROVIDER.models[model],
        thinking=thinking,
        resolved_effort=effort,
        disable_streaming=False,
        timeout=None,
        thinking_budget_tokens=0,
        keys=ProviderKeys({"MIMO_API_KEY": "test-not-a-real-key"}),
    )


def test_thinking_off_rides_extra_body() -> None:
    for context in (_context("none"), _context("", thinking={"type": "disabled"})):
        llm = build(context)
        assert isinstance(llm, ReasoningContentChatModel)
        assert llm.extra_body == {"thinking": {"type": "disabled"}}


def test_the_default_sends_no_extra_body() -> None:
    for effort in ("", "high"):
        llm = build(_context(effort))
        assert isinstance(llm, ReasoningContentChatModel)
        assert llm.extra_body is None
