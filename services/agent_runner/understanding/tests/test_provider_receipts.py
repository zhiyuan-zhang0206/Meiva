# meiva:note
# The operation root reaches real checkpoint, SDK, wire and audit resources.

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import AsyncConnectionPool

from base.agents.history.hierarchy.chunks import Chunk, enqueue_chunk
from base.db import Database
from base.lm.catalog import ModelCatalog
from base.lm.provider_api import ProviderBinding
from base.packages.tests.model_catalog import inert_keys
from base.telemetry.tests.capture.event_capture import EmittedEvents
from services.agent_runner.understanding.chunk_consumer import replay_jobs
from services.agent_runner.understanding.tests.consumer_helpers import consumer_config
from services.agent_runner.understanding.tests.provider_wire import (
    ProviderWire,
    completion,
)


def _checkpoint(database: Database, *, compact_note: bool) -> None:
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {
        "messages": [
            SystemMessage(content="original prefix", id="head"),
            *[
                HumanMessage(
                    content=f"message {i}",
                    id=f"m{i}",
                    additional_kwargs={
                        "meiva_msg_type": "inbound",
                        "meiva_source": "user",
                        "meiva_inbound_body_start": 0,
                        "meiva_created_at": f"2026-10-05T0{i}:00:00+00:00",
                    },
                )
                for i in range(4)
            ],
        ]
    }
    if compact_note:
        checkpoint["channel_values"]["messages"][1] = HumanMessage(
            content="compact request",
            id="m0",
            additional_kwargs={"meiva_msg_type": "compact_request"},
        )
    checkpoint["channel_versions"] = {"messages": "1", "__start__": "1"}
    metadata: CheckpointMetadata = {"source": "input", "step": 1, "parents": {}}
    with database.connect(autocommit=True) as conn:
        conn.execute("INSERT INTO agents (id) VALUES (7)")
        conn.execute(
            "INSERT INTO agents_meta (id, status, config_overlay) VALUES (7, 'idling', %s::jsonb)",
            (json.dumps({"llm_model": "deepseek-flash", "reasoning_effort": "high"}),),
        )
        saver = PostgresSaver(conn)
        saver.setup()
        saver.put(
            {"configurable": {"thread_id": "7", "checkpoint_ns": ""}},
            checkpoint,
            metadata,
            {"messages": "1"},
        )


def _assert_provider_request(body: bytes, efforts: list[str]) -> None:
    request = json.loads(body)
    assert request["model"] == "deepseek-flash"
    assert request["messages"][0]["content"] == "original prefix"
    assert request["messages"][1]["content"] == "message 0"
    assert "[4] human message: message 3" in request["messages"][-1]["content"]
    assert efforts == ["high"]


def _assert_call_receipt(
    call: tuple[Any, ...], *, kind: str, text: str, job_error: str | None
) -> None:
    assert call[:6] == (7, 1, 0, "deepseek-flash", 5, 1)
    if kind == "provider_error":
        assert call[6] is None and "OpenAIInvalidRequestError" in call[9]
        assert job_error is not None and "invalid request" in job_error
    else:
        assert call[6] == text
        assert call[7]["input_tokens"] == 9 and call[7]["output_tokens"] == 2
        assert call[8]["finish_reason"] == "stop"


def _assert_job_outcome(
    job: tuple[Any, ...], nodes: list[tuple[Any, ...]], *, kind: str, emitted_events: EmittedEvents
) -> None:
    assert job[0:2] == ("done" if kind == "done" else "failed", 1)
    if kind == "done":
        assert nodes == [(1, 4, "a real summary")]
        assert job[2] is None
    else:
        assert nodes == []
        assert "understanding_chunk_failed" in emitted_events.names()


@pytest.mark.parametrize("kind", ["done", "refused", "provider_error"])
async def test_real_chunk_provider_receipt_survives_success_and_failure(
    kind: str,
    database: Database,
    aops_pool: AsyncConnectionPool,
    model_catalog: ModelCatalog,
    emitted_events: EmittedEvents,
) -> None:
    _checkpoint(database, compact_note=False)
    config = consumer_config()
    # meiva:note
    # replay deliberately bypasses admission
    config.set_field("understanding_enabled", False)
    config.set_field("understanding_group_corrections", 0)
    config.set_field("hierarchy_model", "deepseek-flash")
    config.set_field("llm_model", "deepseek-flash")
    config.set_field("timezone", "UTC")
    text = '<group first="1" last="4">a real summary</group>' if kind == "done" else "no groups"
    status, reply = (
        (400, b'{"error":{"message":"invalid request","type":"invalid_request_error"}}')
        if kind == "provider_error"
        else (200, completion(text))
    )
    with ProviderWire(status, reply) as wire:
        binding = ProviderBinding(
            prefix="deepseek-",
            display_name="Local SDK transport",
            key_env="DEEPSEEK_API_KEY",
            build=wire.build,
            effort_levels=("high", "max"),
            provider_key="openai",
        )
        catalog = replace(model_catalog, bindings={**model_catalog.bindings, "deepseek-": binding})
        assert await enqueue_chunk(
            aops_pool, 7, compact_version=0, chunk=Chunk(1, 5), end_msg_id="m3"
        )
        await replay_jobs(
            aops_pool,
            database,
            [],
            7,
            catalog=catalog,
            keys=inert_keys(catalog),
            llm_override="",
            config=config,
        )
        assert len(wire.requests) == 1
        _assert_provider_request(wire.requests[0], wire.build_efforts)
        async with aops_pool.connection() as conn:
            jobs = await (
                await conn.execute("SELECT status, attempts, error FROM understanding_chunk_jobs")
            ).fetchall()
            calls = await (
                await conn.execute(
                    "SELECT agent_id, attempt, round, model, prefix_len, start_offset, content,"
                    " usage_metadata, response_metadata, error FROM understanding_chunk_calls"
                )
            ).fetchall()
            nodes = await (
                await conn.execute("SELECT span_start, span_end, text FROM understanding_nodes")
            ).fetchall()
        [job] = jobs
        [call] = calls
        _assert_call_receipt(call, kind=kind, text=text, job_error=job[2])
        _assert_job_outcome(job, nodes, kind=kind, emitted_events=emitted_events)


async def test_generation_does_not_reuse_the_matter_filter_rendering_policy(
    database: Database,
    aops_pool: AsyncConnectionPool,
    model_catalog: ModelCatalog,
    emitted_events: EmittedEvents,
) -> None:
    from base.lm.provider_api import BuildContext

    _checkpoint(database, compact_note=True)
    config = consumer_config()
    config.set_field("understanding_group_corrections", 0)
    config.set_field("message_timestamps", True)
    config.set_field("timezone", "UTC")
    with ProviderWire(200, completion('<group first="1" last="4">summary</group>')) as wire:

        def build(ctx: BuildContext):
            model = wire.build(ctx)
            # meiva:note
            # Matter filtering has already minted its valid UTC policy.
            config.set_field("timezone", "Invalid/AfterMatterFilter")
            return model

        binding = ProviderBinding(
            prefix="deepseek-",
            display_name="Local SDK transport",
            key_env="DEEPSEEK_API_KEY",
            build=build,
            effort_levels=("high", "max"),
            provider_key="openai",
        )
        catalog = replace(model_catalog, bindings={**model_catalog.bindings, "deepseek-": binding})
        assert await enqueue_chunk(
            aops_pool, 7, compact_version=0, chunk=Chunk(1, 5), end_msg_id="m3"
        )
        await replay_jobs(
            aops_pool,
            database,
            [],
            7,
            catalog=catalog,
            keys=inert_keys(catalog),
            llm_override="",
            config=config,
        )
        assert wire.build_efforts == ["high"] and wire.requests == []
        async with aops_pool.connection() as conn:
            cursor = await conn.execute("SELECT status, error FROM understanding_chunk_jobs")
            [(status, error)] = await cursor.fetchall()
            cursor = await conn.execute("SELECT count(*) FROM understanding_chunk_calls")
            assert await cursor.fetchone() == (0,)
        assert status == "failed" and "Invalid/AfterMatterFilter" in error
        assert "understanding_chunk_failed" in emitted_events.names()
