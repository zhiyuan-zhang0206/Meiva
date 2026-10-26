"""Compact summary generation tests: `generate_summary` and the compact boundary stamp.

Split from `agent/tests/test_compact.py`, which holds the shared helpers and the auto-compact hook tests.
"""

from typing import Any

import pytest
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from psycopg_pool import AsyncConnectionPool

from agent.graph.hooks.compact import (
    COMPACTION_INSTRUCTION,
    generate_summary,
)
from agent.llm import execute_code
from agent.tests.test_compact import make_fake_llm, pinned_slices
from base.agents.history.closing_request import METADATA_KEY as CLOSING_METADATA_KEY
from base.agents.history.closing_request import ClosingRequest
from base.clock import SYSTEM_TIMERS
from base.lm.catalog import ModelCatalog
from base.packages.plugin_providers import build_model_catalog


async def _checkpoint(pool: AsyncConnectionPool, checkpoint_id: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint,"
            " metadata) VALUES ('1', '', %s, 'x', '{}'::jsonb, '{}'::jsonb)",
            (checkpoint_id,),
        )


async def _metadata(pool: AsyncConnectionPool, checkpoint_id: str) -> dict[str, Any]:
    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT metadata FROM checkpoints WHERE thread_id='1' AND checkpoint_id=%s",
                (checkpoint_id,),
            )
        ).fetchone()
    assert row is not None
    return row[0]


async def test_stamp_compact_boundary_writes_the_closing_request(
    aops_pool: AsyncConnectionPool,
) -> None:
    from agent.graph.hooks import compact

    closing = ClosingRequest(100, 5, "m1")
    await _checkpoint(aops_pool, "cp-1")
    await compact.stamp_compact_boundary(
        aops_pool, 1, None, closing=closing, understanding_enabled=False, timers=SYSTEM_TIMERS
    )
    await _checkpoint(aops_pool, "cp-2")
    await compact.stamp_compact_boundary(
        aops_pool, 1, understanding_enabled=False, timers=SYSTEM_TIMERS
    )

    assert await _metadata(aops_pool, "cp-1") == {
        "compact_boundary": True,
        CLOSING_METADATA_KEY: closing.to_metadata(),
    }
    assert await _metadata(aops_pool, "cp-2") == {"compact_boundary": True}


# meiva:note
# A completed compaction call records its provider usage as the agent's own.
#
# The regression this catches is removing usage reporting from the compact path, which is the
# largest individual agent provider call. The billing span that usage feeds is pinned by the
# provider billing tests in `base/packages/tests/lm/providers`.
async def test_generate_summary_reports_the_compaction_call_as_agent_usage(
    model_catalog: ModelCatalog, loguru_records: list[dict[str, Any]]
) -> None:
    response = AIMessage(
        content="a complete summary",
        usage_metadata={
            "input_tokens": 1_000,
            "output_tokens": 100,
            "total_tokens": 1_100,
            "input_token_details": {"cache_read": 800},
        },
    )
    llm = make_fake_llm(response=response)
    slices = pinned_slices({"llm_model": "deepseek-flash"})

    assert (
        await generate_summary(
            [HumanMessage(content="conversation")], llm, slices, catalog=model_catalog
        )
        == "a complete summary"
    )

    [record] = [record for record in loguru_records if record["extra"].get("event") == "llm_usage"]
    extra = record["extra"]
    assert (extra["model"], extra["usage_kind"]) == ("deepseek-flash", "agent")
    assert (extra["in_total"], extra["out_total"], extra["cache_read"]) == (1_000, 100, 800)


async def test_generate_summary_includes_whole_conversation():
    """Request = [*whole conversation, HumanMessage(COMPACTION_INSTRUCTION)] — no more hold-out
    tail, model sees every message (including ToolMessage), instruction enters as appended message."""
    ai = AIMessage(
        content="", tool_calls=[{"name": "execute_code", "args": {"code": "x"}, "id": "c1"}]
    )
    convo: list[AnyMessage] = [
        HumanMessage(content="q"),
        ai,
        ToolMessage(content="out", tool_call_id="c1"),
        AIMessage(content="done"),
    ]

    llm = make_fake_llm()
    await generate_summary(convo, llm, pinned_slices(), catalog=build_model_catalog())

    [llm_input] = llm.requests
    assert llm_input[:-1] == convo  # whole conversation, no hold-out
    assert isinstance(llm_input[-1], HumanMessage)
    assert llm_input[-1].text.startswith(f"<system_note>\n{COMPACTION_INSTRUCTION}")


async def test_generate_summary_reuses_conversation_prefix_for_cache():
    """compaction request = main conversation + a final instruction — original SystemMessage kept in place (not replaced with compaction-specific system prompt), bind_tools same as main llm node (tools field participates in prefix rendering); the whole conversation is the prefix of the previous main request, combined they hit the backend prefix cache."""
    sys_msg = SystemMessage(content="<real agent sys prompt>")
    content: list[AnyMessage] = [HumanMessage(content=f"m-{i}") for i in range(5)]

    llm = make_fake_llm()
    await generate_summary([sys_msg, *content], llm, pinned_slices(), catalog=build_model_catalog())

    assert llm.bound_tools == [[execute_code]]
    [llm_input] = llm.requests
    assert llm_input[0] is sys_msg  # original object in-place — same byte-for-byte prefix
    assert llm_input[:-1] == [sys_msg, *content]
    assert llm_input[-1].text.startswith(f"<system_note>\n{COMPACTION_INSTRUCTION}")


async def test_generate_summary_raises_on_empty_llm_text():
    """LLM returns empty text (e.g., defying instruction only gives tool_call) → RuntimeError —
    empty summary cannot be used to replace history."""
    msgs: list[AnyMessage] = [HumanMessage(content=f"m{i}") for i in range(3)]

    with pytest.raises(RuntimeError, match="no text"):
        await generate_summary(
            msgs, make_fake_llm(summary_text=""), pinned_slices(), catalog=build_model_catalog()
        )


async def test_generate_summary_extracts_text_from_block_content():
    """Production provider (thinking enabled) returns block content [thinking, text] —
    summary must be the text block content, not the whole list's repr."""
    msgs: list[AnyMessage] = [HumanMessage(content=f"m{i}") for i in range(3)]
    block_response = AIMessage(
        content=[
            {"type": "thinking", "thinking": "internal reasoning"},
            {"type": "text", "text": "the real summary"},
        ]
    )

    llm = make_fake_llm(response=block_response)
    summary = await generate_summary(msgs, llm, pinned_slices(), catalog=build_model_catalog())
    assert summary == "the real summary"


async def test_generate_summary_raises_on_tool_use_only_block_content():
    """Defying instruction only returns tool_use block (no text block) → response.text is empty →
    RuntimeError — cannot let block list repr become summary."""
    msgs: list[AnyMessage] = [HumanMessage(content=f"m{i}") for i in range(3)]
    tool_only = AIMessage(
        content=[{"type": "tool_use", "id": "c1", "name": "execute_code", "input": {"code": "x"}}]
    )

    with pytest.raises(RuntimeError, match="no text"):
        await generate_summary(
            msgs,
            make_fake_llm(response=tool_only),
            pinned_slices(),
            catalog=build_model_catalog(),
        )


async def test_generate_summary_raises_on_empty_conversation():
    """Only SystemMessage (no conversation) → ValueError — nothing to summarize."""
    with pytest.raises(ValueError, match="empty"):
        await generate_summary(
            [SystemMessage(content="<sys>")],
            make_fake_llm(),
            pinned_slices(),
            catalog=build_model_catalog(),
        )
