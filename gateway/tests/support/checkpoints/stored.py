# meiva:note
# Store a readable checkpoint (a live head or a compact boundary) for the history read tests.

from __future__ import annotations

from typing import cast

from langchain_core.messages import BaseMessage
from langgraph.checkpoint.base import CheckpointMetadata, empty_checkpoint
from langgraph.checkpoint.postgres import PostgresSaver

__all__ = ["put_checkpoint"]


# meiva:note
# Write `messages` as the agent's checkpoint `version`; return the checkpoint id.
#
# A higher version is a newer checkpoint, so the highest one is the live head.
# `boundary=True` marks a retained compact boundary (a sealed segment).
def put_checkpoint(
    db_url: str,
    agent_id: int,
    messages: list[BaseMessage],
    *,
    version: str,
    boundary: bool = False,
) -> str:
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": messages}
    checkpoint["channel_versions"] = {"messages": version, "__start__": "1"}
    metadata: dict[str, object] = {"source": "input", "step": int(version), "parents": {}}
    if boundary:
        metadata["compact_boundary"] = True
    with PostgresSaver.from_conn_string(db_url) as saver:
        saver.setup()
        saved = saver.put(
            config={"configurable": {"thread_id": str(agent_id), "checkpoint_ns": ""}},
            checkpoint=checkpoint,
            metadata=cast(CheckpointMetadata, metadata),
            new_versions={"messages": version},
        )
    return str((saved.get("configurable") or {})["checkpoint_id"])
