# meiva:note
# A stored checkpoint that cannot be read back, for the history read tests.
#
# The checkpoint reader turns any store or deserialize failure into `CheckpointReadError`, and
# the history routes answer it as 503. A test makes that happen with a stored row, not with a
# replaced reader: the messages blob of the checkpoint keeps a valid MessagePack array header (so
# a message count can still be read from its first bytes) and a body no decoder accepts.

from __future__ import annotations

from typing import cast

import psycopg
from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver

__all__ = ["put_unreadable_head"]

# meiva:note
# MessagePack's reserved marker: no decoder accepts it, so the body fails to deserialize.
_UNDECODABLE = b"\xc1"
_FIXARRAY = 0x90
_FIXARRAY_MAX = 15


# meiva:note
# Store a checkpoint with `messages` messages declared and none readable; return its id.
#
# Version ordering decides which checkpoint is the agent's newest, so the default 9 is the
# live head. `boundary=True` makes it a retained compact boundary instead.
def put_unreadable_head(
    db_url: str,
    agent_id: int,
    *,
    messages: int = 3,
    version: str = "9",
    boundary: bool = False,
) -> str:
    if not 0 <= messages <= _FIXARRAY_MAX:
        raise ValueError("the declared message count must fit a MessagePack fixarray header")
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": [AIMessage(content="stored")]}
    checkpoint["channel_versions"] = {"messages": version, "__start__": "1"}
    config = {"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}}
    metadata: dict[str, object] = {"source": "input", "step": int(version), "parents": {}}
    if boundary:
        metadata["compact_boundary"] = True
    with PostgresSaver.from_conn_string(db_url) as saver:
        saver.setup()
        saved = saver.put(
            config=config,  # pyright: ignore[reportArgumentType]
            checkpoint=checkpoint,
            metadata=cast(CheckpointMetadata, metadata),
            new_versions={"messages": version},
        )
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute(
            "UPDATE checkpoint_blobs SET blob = %s"
            " WHERE thread_id = %s AND channel = 'messages' AND version = %s",
            (bytes([_FIXARRAY + messages]) + _UNDECODABLE, str(agent_id), version),
        )
    return str((saved.get("configurable") or {})["checkpoint_id"])
