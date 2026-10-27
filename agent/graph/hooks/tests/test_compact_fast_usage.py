from typing import cast

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage

from agent.graph.hooks.compact import generate_summary
from agent.graph.hooks.tests.scripted_model import ScriptedModel
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.log.tests.log_records import LogRecords
from base.packages.plugin_providers import build_model_catalog
from base.paths import meiva_home


async def test_compaction_preserves_fast_accounting_id(loguru_records: LogRecords) -> None:
    slices = AgentSlices.resolve({"llm_model": "gpt-6.1-sol-fast"}, default_reader=defaults())
    response = AIMessage(
        content="summary",
        response_metadata={"service_tier": "fast"},
        usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
    )
    llm = cast(BaseChatModel, ScriptedModel(response))

    summary = await generate_summary(
        [HumanMessage(content="history")],
        llm,
        slices,
        catalog=build_model_catalog(home=meiva_home()),
    )

    assert summary == "summary"
    (usage,) = loguru_records.events("llm_usage")
    assert usage["extra"]["model"] == "gpt-6.1-sol-fast"
