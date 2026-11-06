from datetime import UTC, datetime

import psycopg
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg_pool import AsyncConnectionPool

from agent.graph.hooks.user_delivery import with_pending_deliveries
from agent.graph.llm.node import llm_node
from agent.graph.llm.tests.llm_support import ScriptedModel
from agent.graph.llm_errors import FatalLLMStreamError
from agent.graph.tests.test_llm_helpers import make_runtime
from agent.state import AgentState
from base.agents.context import HostedTurnResources
from base.agents.messages.kwargs import read_meiva_kwargs
from base.agents.to_user.delivery.store import publish
from base.api_contracts.user_delivery import PublishMessage, UserDeliveryMode
from base.clock.tests.seeded_rng import seeded_rng
from base.db import create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.tests.live_capture import LiveEvents
from base.events.live.tests.live_capture import live_events as live_events
from meiva.tests.fixtures.pin_agent import hosted_resources as hosted_resources


def final_model() -> ScriptedModel:
    return ScriptedModel(
        scripts=[
            [
                AIMessageChunk(
                    content="Private wrap-up",
                    response_metadata={"model_provider": "anthropic", "stop_reason": "end_turn"},
                    usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                )
            ]
            for _ in range(3)
        ]
    )


async def test_visible_reply_capture_and_checkpoint_metadata(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    agent_id = create_agent(db_conn)
    runtime = make_runtime(
        resources=hosted_resources,
        llm=final_model(),
        event_publisher=live_events.publisher,
        database_gate=database_gate,
        pins={"user_delivery_mode": "naive"},
        ops_pool=aops_pool,
    )
    result = await llm_node(
        AgentState(messages=[HumanMessage(content="Answer directly")]),
        runtime,
        {"configurable": {"thread_id": str(agent_id)}},
        rng=seeded_rng(),
    )
    message = AgentState.model_validate(result.update).messages[-1]
    assert type(read_meiva_kwargs(message).get("meiva_user_delivery_mode")) is str
    serde = JsonPlusSerializer()
    restored = AIMessage.model_validate(serde.loads_typed(serde.dumps_typed(message)))
    assert read_meiva_kwargs(restored).get("meiva_user_delivery_mode") == "naive"
    assert db_conn.execute(
        "SELECT mode,content FROM agent_user_messages WHERE agent_id=%s", (agent_id,)
    ).fetchall() == [("naive", "Private wrap-up")]


@pytest.mark.parametrize("delivery", ["publish", "ask", "no_update"])
async def test_natural_end_requires_publication_and_reminds_once(
    delivery: str,
    db_conn: psycopg.Connection,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    agent_id = create_agent(db_conn)
    runtime = make_runtime(
        resources=hosted_resources,
        llm=final_model(),
        event_publisher=live_events.publisher,
        database_gate=database_gate,
        pins={"user_delivery_mode": "sdk"},
    )
    state = AgentState(messages=[HumanMessage(content="Do the work")], user_delivery_baseline=0)
    config: RunnableConfig = {"configurable": {"thread_id": str(agent_id)}}
    first = await llm_node(state, runtime, config, rng=seeded_rng())
    first_update = AgentState.model_validate(first.update)
    assert first.goto == "before_llm"
    assert first_update.halted is False
    assert "without publishing" in first_update.messages[-1].text
    corrected = AgentState(
        messages=[*state.messages, *first_update.messages], user_delivery_baseline=0
    )
    with pytest.raises(FatalLLMStreamError, match="SDK delivery contract violated"):
        await llm_node(corrected, runtime, config, rng=seeded_rng())
    assert db_conn.execute(
        "SELECT count(*) FROM agent_user_messages WHERE agent_id=%s", (agent_id,)
    ).fetchone() == (0,)

    if delivery == "ask":
        db_conn.execute(
            "INSERT INTO agent_notices(agent_id,local_id,title,priority,require_response,expire_at,question) "
            "VALUES (%s,0,'Choose scope','P2',true,NULL,'{\"type\":\"text\"}')",
            (agent_id,),
        )
    else:
        publish(
            db_conn,
            agent_id,
            "milestone",
            PublishMessage(content=None if delivery == "no_update" else "Finished", suggestions=[]),
            mode=UserDeliveryMode.SDK,
            created_at=datetime.now(UTC),
        )
    db_conn.commit()
    third = await llm_node(state, runtime, config, rng=seeded_rng())
    third_update = AgentState.model_validate(third.update)
    assert third.goto == "after_exec" and len(third_update.messages) == 1


async def test_compacted_context_restores_unread_and_unanswered_deliveries(
    db_conn: psycopg.Connection,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agent_notices(agent_id,local_id,title,priority,require_response,expire_at,question) "
        "VALUES (%s,0,'Approve the smaller scope?','P2',true,NULL,'{\"type\":\"text\"}')",
        (agent_id,),
    )
    db_conn.commit()
    runtime = make_runtime(
        resources=hosted_resources, database_gate=database_gate, pins={"user_delivery_mode": "sdk"}
    )
    compacted = [HumanMessage(content="The workspace overview was rewritten.")]
    restored = await with_pending_deliveries(runtime.context, agent_id, compacted)
    assert len(restored) == 2
    assert "Approve the smaller scope?" in restored[-1].content
    assert "Open Ask" in restored[-1].content
    assert len(compacted) == 1
