"""The checkpoint readers run as the least-privilege `meiva_runner` login.

Split out of `base/agents/incarnation/tests/test_runner_role.py`: it exercises the history
readers, which sit above incarnation, so it lives with the history tests and reuses the runner
database fixture from there.
"""

from __future__ import annotations

from typing import cast

from langgraph.checkpoint.postgres import PostgresSaver

from base.agents.incarnation.tests.test_runner_role import grant_runner, runner_url
from base.agents.incarnation.tests.test_runner_role import runner_db as runner_db
from base.db.code_version_gate import ProcessDbGate


def test_checkpoint_reads_need_crud_not_schema_ddl(
    runner_db: str, database_gate: ProcessDbGate
) -> None:
    """Both checkpoint readers work as ``meiva_runner`` without schema CREATE.

    The checkpoint schema is an install / migration concern.  A reader calling
    ``PostgresSaver.setup()`` first issues ``CREATE TABLE IF NOT EXISTS`` and
    PostgreSQL correctly rejects it for this least-privilege runtime role even
    when every table already exists.  Production then loses timeline / inspect
    history despite the role holding all CRUD privileges the read itself needs.
    """
    from dataclasses import replace

    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint

    from base.agents.history.checkpoint import (
        load_checkpoint_messages,
        load_checkpoint_messages_by_trace,
    )
    from base.db import Database
    from base.db.config import db_config_from_settings

    grant_runner(runner_db)

    trace_id = "f" * 32
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": [HumanMessage(content="runtime read")]}
    checkpoint["channel_versions"] = {"messages": "1", "__start__": "1"}
    with PostgresSaver.from_conn_string(runner_db) as saver:
        saved = saver.put(
            config={"configurable": {"thread_id": "73", "checkpoint_ns": ""}},
            checkpoint=checkpoint,
            metadata=cast(
                CheckpointMetadata,
                {"source": "input", "step": 1, "parents": {}, "trace_id": trace_id},
            ),
            new_versions={"messages": "1"},
        )

    runner_database = Database(
        replace(db_config_from_settings(), db_url=runner_url(runner_db)),
        gate=database_gate,
    )
    current = load_checkpoint_messages(runner_database, 73)
    checkpoint_id, traced = load_checkpoint_messages_by_trace(runner_database, 73, trace_id)

    # This legacy checkpoint bypasses Pregel's ID assignment. Reading it
    # records provenance without requiring DDL or rewriting stored data.
    expected = HumanMessage(
        content="runtime read", additional_kwargs={"meiva_ephemeral_message_id": True}
    )
    assert current == [expected]
    assert checkpoint_id == saved["configurable"]["checkpoint_id"]  # pyright: ignore[reportTypedDictNotRequiredAccess]
    assert traced == [expected]
    with PostgresSaver.from_conn_string(runner_url(runner_db)) as saver:
        original = saver.get_tuple(saved)
    assert original is not None
    assert original.checkpoint["channel_values"]["messages"] == [
        HumanMessage(content="runtime read")
    ]


def test_runner_role_lazily_stamps_and_reuses_compact_segment_shape(
    runner_db: str, database_gate: ProcessDbGate
) -> None:
    from dataclasses import replace

    import psycopg
    from langchain_core.messages import HumanMessage, SystemMessage
    from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint

    from base.agents.history.checkpoint_segment import (
        SEGMENT_SHAPE_METADATA_KEY,
        BoundarySegmentShape,
        open_segment_reader,
    )
    from base.db import Database
    from base.db.config import db_config_from_settings

    grant_runner(runner_db)

    boundary_ckpt = empty_checkpoint()
    boundary_ckpt["channel_values"] = {
        "messages": [
            SystemMessage(content="sys", id="sys-0"),
            HumanMessage(content="first", id="m0"),
            HumanMessage(content="legacy without id"),
        ]
    }
    boundary_ckpt["channel_versions"] = {"messages": "1", "__start__": "1"}
    with PostgresSaver.from_conn_string(runner_db) as saver:
        saved_boundary = saver.put(
            config={"configurable": {"thread_id": "74", "checkpoint_ns": ""}},
            checkpoint=boundary_ckpt,
            metadata=cast(
                CheckpointMetadata,
                {
                    "source": "loop",
                    "step": 1,
                    "parents": {},
                    "compact_boundary": True,
                    "trace_id": "a" * 32,
                },
            ),
            new_versions={"messages": "1"},
        )
    configurable = saved_boundary.get("configurable") or {}
    boundary_id = str(configurable["checkpoint_id"])

    runner_database = Database(
        replace(db_config_from_settings(), db_url=runner_url(runner_db)),
        gate=database_gate,
    )
    with open_segment_reader(runner_database, 74) as reader:
        shapes = reader.load_boundary_shapes(end_index=2)
    assert shapes == (
        BoundarySegmentShape(
            checkpoint_id=boundary_id,
            head_count=1,
            body_len=2,
            end_id_at_index="m0",
        ),
    )

    with psycopg.connect(runner_db) as admin_conn:
        row = admin_conn.execute(
            "SELECT metadata FROM checkpoints WHERE thread_id = '74' AND checkpoint_id = %s",
            (boundary_id,),
        ).fetchone()
        assert row is not None
        metadata = cast(dict[str, object], row[0])
        assert metadata["compact_boundary"] is True
        assert metadata["trace_id"] == "a" * 32
        assert metadata[SEGMENT_SHAPE_METADATA_KEY] == {
            "v": 1,
            "head": 1,
            "body": 2,
            "ids": ["m0", None],
        }
        admin_conn.execute("DELETE FROM checkpoint_blobs WHERE thread_id = '74'")
        admin_conn.commit()

    with open_segment_reader(runner_database, 74) as reader:
        assert reader.load_boundary_shapes(end_index=2) == shapes
        assert reader.load_boundary_shapes(end_index=3)[0].end_id_at_index is None
        assert reader.load_boundary_shapes(end_index=0)[0].end_id_at_index is None
