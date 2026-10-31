from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any, TypedDict, cast
from uuid import uuid4

import psycopg
import pytest
from langchain_core.messages import HumanMessage, RemoveMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.channels.binop import _get_overwrite
from langgraph.checkpoint.base import (
    CheckpointMetadata,
    CheckpointTuple,
    DeltaChannelHistory,
    writes_sort_key,
)
from langgraph.checkpoint.base.id import uuid6
from langgraph.checkpoint.postgres import PostgresSaver as UpstreamPostgresSaver
from langgraph.checkpoint.postgres.base import BasePostgresSaver, _DeltaStage2Row
from langgraph.checkpoint.serde.base import SerializerProtocol
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.serde.types import _DeltaSnapshot
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from base.agents.history.checkpoint import (
    CheckpointReadError,
    load_checkpoint_messages,
    load_checkpoint_messages_segment,
)
from base.agents.history.checkpoint_postgres_walks import (
    HistoryAsyncPostgresSaver as AsyncPostgresSaver,
)
from base.agents.history.checkpoint_postgres_walks import HistoryPostgresSaver as PostgresSaver
from base.agents.history.checkpoint_postgres_walks import (
    PrivateSurface,
    installed_private_surface,
    surface_of,
    verify_private_surface,
)
from base.agents.history.delta_read_compat import wrap_saver_reads_with_delta_reconstruction
from base.config import settings
from base.db import Database
from base.db.code_version_gate import ProcessDbGate


def _db(database_gate: ProcessDbGate) -> Database:
    return Database.from_settings(gate=database_gate)


def _checkpoint_id(config: RunnableConfig) -> str:
    identity = config.get("configurable")
    assert identity is not None
    return cast(str, identity["checkpoint_id"])


def _append_delta_checkpoint(
    saver: UpstreamPostgresSaver, *, thread: str, step: int, parent: RunnableConfig | None
) -> RunnableConfig:
    configurable: dict[str, Any] = {"thread_id": thread, "checkpoint_ns": ""}
    if parent is not None:
        assert "configurable" in parent
        configurable["checkpoint_id"] = parent["configurable"]["checkpoint_id"]
    config: RunnableConfig = {"configurable": configurable}
    value = (
        [SystemMessage(content="seed", id="seed")]
        if step == 0
        else [HumanMessage(content=f"write-{step}", id=f"write-{step}")]
    )
    metadata: dict[str, Any] = {
        "source": "loop",
        "step": step,
        "writes": {},
        "parents": {},
        "counters_since_delta_snapshot": {"messages": (step, 0)},
    }
    if step == 2:
        metadata["compact_boundary"] = True
    saved = saver.put(
        config,
        {
            "v": 1,
            "id": str(uuid6(clock_seq=-1)),
            "ts": "",
            "channel_values": {"messages": _DeltaSnapshot(value)} if step == 0 else {},
            "channel_versions": {"messages": str(step + 1)},
            "versions_seen": {},
            "updated_channels": None,
        },
        cast(CheckpointMetadata, metadata),
        {"messages": str(step + 1)} if step == 0 else {},
    )
    if step:
        saver.put_writes(saved, [("messages", value)], str(uuid4()))
    return saved


def _write_steps(
    saver: UpstreamPostgresSaver, thread: str, count: int, parent: RunnableConfig | None
) -> list[RunnableConfig]:
    configs: list[RunnableConfig] = []
    for step in range(count):
        parent = _append_delta_checkpoint(saver, thread=thread, step=step, parent=parent)
        configs.append(parent)
    return configs


async def test_missing_delta_parent_fails_sync_async_and_public_reader(
    db_conn: psycopg.Connection, database_gate: ProcessDbGate
) -> None:
    thread = "6111"
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        configs = _write_steps(saver, thread, 4, None)
        missing = _checkpoint_id(configs[2])
        db_conn.execute(
            "DELETE FROM checkpoints WHERE thread_id=%s AND checkpoint_id=%s", (thread, missing)
        )
        db_conn.commit()
        with pytest.raises(RuntimeError, match="delta messages ancestry is incomplete"):
            _history(saver, configs[-1])
    async with AsyncPostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        with pytest.raises(RuntimeError, match="delta messages ancestry is incomplete"):
            await saver.aget_delta_channel_history(config=configs[-1], channels=["messages"])
    with pytest.raises(CheckpointReadError) as failed:
        load_checkpoint_messages(_db(database_gate=database_gate), int(thread))
    assert isinstance(failed.value.__cause__, RuntimeError)
    assert str(missing) in str(failed.value.__cause__)
    assert db_conn.execute(
        "SELECT parent_checkpoint_id FROM checkpoints WHERE thread_id=%s AND checkpoint_id=%s",
        (thread, _checkpoint_id(configs[-1])),
    ).fetchone() == (missing,)


def test_real_message_reset_can_cut_off_a_missing_older_parent(
    db_conn: psycopg.Connection, database_gate: ProcessDbGate
) -> None:
    thread = "6112"
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        configs = _write_steps(saver, thread, 4, None)
        saver.put_writes(
            configs[2],
            [
                (
                    "messages",
                    [
                        RemoveMessage(id=REMOVE_ALL_MESSAGES),
                        HumanMessage(id="kept", content="kept"),
                    ],
                )
            ],
            "reset-task",
        )
        db_conn.execute(
            "DELETE FROM checkpoints WHERE thread_id=%s AND checkpoint_id=%s",
            (thread, _checkpoint_id(configs[1])),
        )
        db_conn.commit()
        entry = _history(saver, configs[-1])
        assert "seed" not in entry
        assert any(
            isinstance(message, RemoveMessage) for write in entry["writes"] for message in write[2]
        )
    assert [
        message.id
        for message in load_checkpoint_messages(_db(database_gate=database_gate), int(thread))
    ] == ["kept"]


def _write_numbers(entry: DeltaChannelHistory) -> list[int]:
    return [
        int(cast(str, cast(list[HumanMessage], write[2])[0].content).removeprefix("write-"))
        for write in entry["writes"]
    ]


def _history(saver: UpstreamPostgresSaver, config: RunnableConfig) -> DeltaChannelHistory:
    return saver.get_delta_channel_history(config=config, channels=["messages"])["messages"]


def _assert_walk_has_writes_and_seed(entry: DeltaChannelHistory, expected: list[int]) -> None:
    assert _write_numbers(entry) == expected
    assert "seed" in entry and isinstance(entry["seed"], _DeltaSnapshot)


def _assert_early_checkpoints_walk_back_to_root(
    saver: UpstreamPostgresSaver, first: list[RunnableConfig]
) -> None:
    assert _history(saver, first[0]) == {"writes": []}
    for target, expected in ((1, []), (2, [1]), (3, [1, 2]), (5, [1, 2, 3, 4])):
        _assert_walk_has_writes_and_seed(_history(saver, first[target]), expected)


def _append_newer_checkpoints_across_page_boundary(
    saver: UpstreamPostgresSaver, thread: str, first: list[RunnableConfig], target: RunnableConfig
) -> None:
    parent = first[-1]
    for step in range(6, 1027):
        parent = _append_delta_checkpoint(saver, thread=thread, step=step, parent=parent)
        if step == 1025:
            _assert_walk_has_writes_and_seed(_history(saver, target), [1])
    _assert_walk_has_writes_and_seed(_history(saver, target), [1])


async def test_historical_walk_page_boundary_and_compact_segment(
    db_conn: psycopg.Connection, database_gate: ProcessDbGate
) -> None:
    thread = "6380"
    with PostgresSaver.from_conn_string(settings.data_plane.db_url) as saver:
        first = _write_steps(saver, thread, 6, None)
        _assert_early_checkpoints_walk_back_to_root(saver, first)
        target = first[2]
        _append_newer_checkpoints_across_page_boundary(saver, thread, first, target)

    async with AsyncPostgresSaver.from_conn_string(settings.data_plane.db_url) as async_saver:
        async_entry = (
            await async_saver.aget_delta_channel_history(config=target, channels=["messages"])
        )["messages"]
    _assert_walk_has_writes_and_seed(async_entry, [1])

    assert "configurable" in target
    segment = load_checkpoint_messages_segment(
        _db(database_gate=database_gate),
        int(thread),
        cast(str, target["configurable"]["checkpoint_id"]),
    )
    assert [message.id for message in segment] == ["write-1"]


# meiva:note
# langgraph#8556 owns the walk cursor now; shadowing the pager here would silently bring back the historical-target bug.
def test_meiva_keeps_only_the_message_reset_reader() -> None:
    assert "_try_advance_walks" not in vars(PostgresSaver)
    assert "_try_advance_walks" not in vars(AsyncPostgresSaver)
    assert "get_delta_channel_history" in vars(PostgresSaver)


def _config() -> RunnableConfig:
    return {
        "configurable": {
            "thread_id": "thread-a",
            "checkpoint_id": "checkpoint-a",
            "checkpoint_ns": "",
        }
    }


def _failing_saver(*, serde: SerializerProtocol | None = None) -> AsyncPostgresSaver:
    saver = AsyncPostgresSaver(cast(Any, object()), serde=serde)

    async def raw_tuple(config: RunnableConfig) -> CheckpointTuple:
        return CheckpointTuple(
            config,
            cast(
                Any,
                {"id": "checkpoint-a", "channel_values": {}, "channel_versions": {"messages": "v"}},
            ),
            cast(Any, {"counters_since_delta_snapshot": {"messages": 1}}),
            None,
            [],
        )

    saver.aget_tuple = raw_tuple
    wrap_saver_reads_with_delta_reconstruction(saver)
    return saver


async def test_delta_read_timeout_emits_partial_span(loguru_records: list[Any]) -> None:
    saver = _failing_saver()

    async def timed_out(*, config: RunnableConfig, channels: Sequence[str]) -> Any:
        raise TimeoutError("history timed out")

    saver.aget_delta_channel_history = timed_out
    with pytest.raises(TimeoutError, match="history timed out"):
        await saver.aget_tuple(_config())
    spans = [
        record["extra"]
        for record in loguru_records
        if record["extra"].get("event") == "delta_read_compat"
    ]
    assert len(spans) == 1
    assert spans[0]["outcome"] == "error"
    assert spans[0]["failed_phase"] == "history_read"
    assert spans[0]["error_type"] == "TimeoutError"
    assert spans[0]["elapsed_ms"] >= spans[0]["history_read_ms"] >= 0
    assert spans[0]["message_count"] is None
    assert spans[0]["cache_hit"] is None


async def test_delta_read_decode_error_emits_partial_span(loguru_records: list[Any]) -> None:
    saver = _failing_saver()

    async def bad_history(*, config: RunnableConfig, channels: Sequence[str]) -> Any:
        return saver._build_delta_channels_writes_history(
            channels=["messages"],
            chain_by_ch={"messages": ["checkpoint-a"]},
            seed_ver_by_ch={},
            seed_inline_by_ch={},
            stage2_rows=[
                {
                    "channel": "messages",
                    "_kind": "w",
                    "checkpoint_id": "checkpoint-a",
                    "type": "msgpack",
                    "blob": b"\xc1",
                    "task_id": "task",
                    "task_path": "",
                    "idx": 0,
                }
            ],
        )

    saver.aget_delta_channel_history = bad_history
    with pytest.raises(ValueError):
        await saver.aget_tuple(_config())
    spans = [
        record["extra"]
        for record in loguru_records
        if record["extra"].get("event") == "delta_read_compat"
    ]
    assert len(spans) == 1
    assert spans[0]["outcome"] == "error"
    assert spans[0]["failed_phase"] == "history_read"
    assert spans[0]["error_type"] == "ValueError"
    assert spans[0]["elapsed_ms"] >= spans[0]["history_read_ms"] >= 0
    assert "decode_ms" not in spans[0] and "reset_decode_ms" not in spans[0]
    assert spans[0]["message_count"] is None
    assert spans[0]["cache_hit"] is None


async def test_checkpoint_reads_leave_shared_serializer_unchanged(
    loguru_records: list[Any],
) -> None:
    shared_serde = JsonPlusSerializer()
    decode = shared_serde.loads_typed
    first = AsyncPostgresSaver(cast(Any, object()), serde=shared_serde)
    wrap_saver_reads_with_delta_reconstruction(first)
    saver = _failing_saver(serde=shared_serde)
    assert saver.serde is first.serde
    assert shared_serde.loads_typed == decode

    async def bad_history(*, config: RunnableConfig, channels: Sequence[str]) -> Any:
        return saver._build_delta_channels_writes_history(
            channels=["messages"],
            chain_by_ch={"messages": ["checkpoint-a"]},
            seed_ver_by_ch={},
            seed_inline_by_ch={},
            stage2_rows=[
                {
                    "channel": "messages",
                    "_kind": "w",
                    "checkpoint_id": "checkpoint-a",
                    "type": "msgpack",
                    "blob": b"\xc1",
                    "task_id": "task",
                    "task_path": "",
                    "idx": 0,
                }
            ],
        )

    saver.aget_delta_channel_history = bad_history
    with pytest.raises(ValueError):
        await saver.aget_tuple(_config())
    spans = [
        record["extra"]
        for record in loguru_records
        if record["extra"].get("event") == "delta_read_compat"
    ]
    assert len(spans) == 1
    assert spans[0]["failed_phase"] == "history_read"
    assert spans[0]["error_type"] == "ValueError"
    assert shared_serde.loads_typed == decode


# meiva:note
# The adapter reads checkpoint-postgres's private extension surface, and a saver refuses to be
# built over a surface this file was not written against. Each case states one shape a release
# could change and builds a saver with it: the refusal must come at construction, before any
# connection is used, and never as a mis-cut reset during a replay.
def test_the_installed_private_surface_is_the_one_this_adapter_reads() -> None:
    verify_private_surface(installed_private_surface())


def test_a_renamed_stage2_row_field_refuses_the_sync_saver_construction() -> None:
    class RenamedRow(TypedDict):
        _kind: str
        checkpoint_id: str | None
        channel: str | None
        type: str | None
        blob: bytes | None
        task_id: str | None
        task_path: str | None
        write_idx: int | None
        version: str | None

    surface = surface_of(
        stage2_row=RenamedRow,
        sort_key=writes_sort_key,
        overwrite_reader=_get_overwrite,
        base_saver=BasePostgresSaver,
    )
    assert surface.stage2_row_fields == (
        "_kind",
        "checkpoint_id",
        "channel",
        "type",
        "blob",
        "task_id",
        "task_path",
        "write_idx",
        "version",
    )
    with pytest.raises(RuntimeError, match="stage-2 row changed"):
        PostgresSaver(cast(Any, object()), surface=surface)


def test_a_changed_writes_sort_key_refuses_the_async_saver_construction() -> None:
    def sort_by_task_id(task_id: str, task_path: str, idx: int = 0) -> tuple[str, str, int]:
        return task_path, task_id, idx

    surface = surface_of(
        stage2_row=_DeltaStage2Row,
        sort_key=sort_by_task_id,
        overwrite_reader=_get_overwrite,
        base_saver=BasePostgresSaver,
    )
    with pytest.raises(RuntimeError, match="writes_sort_key changed"):
        AsyncPostgresSaver(cast(Any, object()), surface=surface)


def test_a_changed_advance_walks_signature_refuses_the_saver_construction() -> None:
    surface: PrivateSurface = replace(
        installed_private_surface(), advance_walks_parameters=("target_id",)
    )
    with pytest.raises(RuntimeError, match="_try_advance_walks changed"):
        PostgresSaver(cast(Any, object()), surface=surface)


def test_a_refusal_names_the_shape_it_found() -> None:
    surface: PrivateSurface = replace(
        installed_private_surface(), stage2_row_fields=("idx", "version")
    )
    with pytest.raises(RuntimeError) as refused:
        verify_private_surface(surface)
    assert "['idx', 'version']" in str(refused.value)
