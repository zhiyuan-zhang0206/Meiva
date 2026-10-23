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
