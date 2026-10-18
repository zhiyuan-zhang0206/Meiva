from dataclasses import replace
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from psycopg_pool import AsyncConnectionPool

from agent.impersonation_handoff import flush_checkpoint
from agent.state import ContextReset
from base.agents.compaction.models import CompactHeldError
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from base.packages.tests.model_catalog import AddBindings
from gateway.tests.lifecycle.guarded_compact.host.admission import admit
from gateway.tests.test_idempotency import client as client
from services.agent_runner.agent_host.invocation.compact import apply as compact_apply
from services.agent_runner.agent_host.invocation.compact.checkpoint import (
    cold_reader,
    verbatim_suffix,
)
from services.agent_runner.agent_host.tests.guarded_compact_support import SummaryModel


@pytest.mark.parametrize("corruption", ["old_source", "reset_tail"])
async def test_actual_cold_materialization_gap_never_acknowledges_marker_alone(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    add_bindings: AddBindings,
    corruption: str,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    model = SummaryModel(responses=["Original summary with enough useful content. " * 100])
    binding = model_catalog.bindings["gpt-"]
    model_catalog = add_bindings(
        model_catalog, {"gpt-": replace(binding, build_single_attempt=lambda _: model)}
    )
    accepted = await admit(
        db_conn, aops_pool, client, monkeypatch, catalog=model_catalog, database_gate=database_gate
    )
    acknowledge = compact_apply.acknowledge
    observed: list[bool] = []

    async def missing_materialization(*args: Any, **kwargs: Any) -> bool:
        snapshot = await cold_reader(accepted.saver).aget_tuple(accepted.config)
        assert snapshot is not None
        if snapshot.checkpoint["channel_values"].get("native_compact") is not None:
            message = HumanMessage(id="source-history", content="Retain this task")
            update = (
                {"messages": [message]}
                if corruption == "old_source"
                else {"context_reset": ContextReset(tail=[message])}
            )
            await accepted.host._graph.aupdate_state(accepted.config, update, as_node="claim")
            await flush_checkpoint(accepted.saver, accepted.agent)
            observed.append(True)
        return await acknowledge(*args, **kwargs)

    monkeypatch.setattr(compact_apply, "acknowledge", missing_materialization)
    with pytest.raises(CompactHeldError, match="materialized application"):
        await accepted.host.run_turn(accepted.agent)
    status = accepted.status(client)
    assert observed == [True] and model.calls == 1
    assert status["outcome"] == "uncertain" and status["checkpoint_id"] is None
    assert not status["continuation_released"] and status["result_available"]
    cold = await cold_reader(accepted.saver).aget_tuple(accepted.config)
    assert cold is not None and cold.checkpoint["channel_values"]["native_compact"] is not None


def test_a_matching_window_is_verbatim() -> None:
    source = [HumanMessage(id="m1", content="old"), AIMessage(id="m2", content="reply")]

    assert verbatim_suffix(source, source)
    assert verbatim_suffix([source[1]], source)


def test_a_rewritten_message_with_the_same_id_is_not_verbatim() -> None:
    source = [HumanMessage(id="m1", content="old"), AIMessage(id="m2", content="reply")]

    assert not verbatim_suffix([source[0], AIMessage(id="m2", content="rewritten")], source)


def test_a_window_that_is_not_the_source_tail_is_not_verbatim() -> None:
    source = [HumanMessage(id="m1", content="old"), AIMessage(id="m2", content="reply")]

    assert not verbatim_suffix([source[0]], source)
    assert not verbatim_suffix([HumanMessage(id="m9", content="fabricated")], source)


def test_the_filtered_tail_still_counts_as_verbatim() -> None:
    source = [
        HumanMessage(id="m1", content="old"),
        AIMessage(id="m2", content="reply"),
        AIMessage(
            id="m3",
            content="",
            tool_calls=[
                {
                    "name": "execute_code",
                    "args": {"code": "meiva.self.compact('x')"},
                    "id": "c9",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(
            id="m4",
            content="[system halt] You just called meiva.self.compact",
            tool_call_id="c9",
        ),
    ]

    assert verbatim_suffix([source[0], source[1]], source)
