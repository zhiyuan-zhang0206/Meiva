from typing import cast

import httpx2
import openai
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from agent.graph.hooks import compact
from agent.graph.hooks.tests.scripted_model import ScriptedModel
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.packages.plugin_providers import build_model_catalog
from base.paths import meiva_home


@pytest.mark.parametrize("provider_cause", [False, True])
async def test_compaction_programming_error_stops_once_without_history_change(
    provider_cause: bool,
    model_catalog: ModelCatalog,
) -> None:
    messages: list[AnyMessage] = [HumanMessage(content="Original history")]
    original = list(messages)
    error = TypeError("summary implementation broke")
    if provider_cause:
        response = httpx2.Response(400, request=httpx2.Request("POST", "https://audit.invalid"))
        error.__cause__ = openai.BadRequestError("rejected", response=response, body=None)
    # meiva:note
    # A second attempt would find a valid summary, so only a single attempt ends in the error.
    llm = cast(
        BaseChatModel,
        ScriptedModel(error, AIMessage(content="summary " * compact.COMPACT_MIN_SUMMARY_CHARS)),
    )

    with pytest.raises(TypeError) as raised:
        await compact.auto_compact_summary(
            messages,
            llm,
            content_count=1,
            slices=AgentSlices.resolve(default_reader=defaults()),
            catalog=model_catalog,
        )

    assert raised.value is error
    assert messages == original


@pytest.mark.parametrize("first_summary", ["", "short"])
async def test_compaction_keeps_explicit_empty_and_short_summary_recovery(
    first_summary: str,
) -> None:
    messages: list[AnyMessage] = [HumanMessage(content="Original history")]
    expected = "summary " * compact.COMPACT_MIN_SUMMARY_CHARS
    llm = cast(
        BaseChatModel,
        ScriptedModel(AIMessage(content=first_summary), AIMessage(content=expected)),
    )
    slices = AgentSlices.resolve(default_reader=defaults())

    result = await compact.auto_compact_summary(
        messages, llm, 1, slices, catalog=build_model_catalog(home=meiva_home())
    )

    assert result == expected
    assert messages == [HumanMessage(content="Original history")]
