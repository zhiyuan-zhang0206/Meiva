from typing import Any, cast

import pytest
from langchain_core.messages import HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from base.lm.provider_api import BuildContext
from meiva_builtins.plugins.lm_google.provider import PROVIDER, chat_model_kwargs


@pytest.mark.parametrize("model", sorted(PROVIDER.models))
def test_request_has_no_sampling_parameters(model: str) -> None:
    ctx = BuildContext(
        model=model,
        spec=PROVIDER.models[model],
        thinking=None,
        resolved_effort="",
        disable_streaming=False,
        timeout=None,
        thinking_budget_tokens=0,
    )
    llm = ChatGoogleGenerativeAI(**chat_model_kwargs(ctx, api_key="test-not-a-real-key"))

    config = cast(Any, llm)._prepare_request([HumanMessage(content="hi")])["config"]

    assert llm.temperature is None
    assert config.temperature is None
    assert config.top_p is None
    assert config.top_k is None


@pytest.mark.parametrize("model", ("gemini-3.5-flash", "gemini-3.7-flash"))
def test_deprecated_flash_models_are_superseded_by_3_8_flash(model: str) -> None:
    spec = PROVIDER.models[model]
    assert spec.spawnable is True
    assert spec.superseded_by == "gemini-3.8-flash"


# meiva:note
# gemini-4 emits no bytes before its first thought/content chunk and the
# google-genai SDK retries a 429/503 internally for ~31s with no bytes either,
# so the 30s shared TTFT floor turns a throttled-but-healthy request into a
# fatal stall (agent 6933, 2026-10-18: the turn died at 30.02s with zero chunks).
# meiva:link meiva_builtins/plugins/docs/plugins.meiva.okf.md
def test_gemini_4_argon_ttft_budget_clears_the_sdks_silent_retry_window() -> None:
    tuning = PROVIDER.models["gemini-4-argon"].tuning
    assert tuning is not None
    assert tuning.llm_stream_ttft_timeout_seconds == 90.0
