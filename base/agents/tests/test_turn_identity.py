from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from base.agents.context import MeivaContext
from base.native_process.runtime_incarnation import RuntimeIncarnation


def test_wrong_agent_incarnation_is_rejected() -> None:
    incarnation = RuntimeIncarnation(7, uuid4(), uuid4())
    with pytest.raises(RuntimeError, match="different agent"):
        incarnation.require_agent(8)
    assert incarnation.require_agent(7) is incarnation


async def test_context_branches_keep_the_incarnation_after_parent_replacement() -> None:
    incarnation = RuntimeIncarnation(7, uuid4(), uuid4())
    original = MeivaContext(original_incarnation=incarnation)
    child_ready, release_child = asyncio.Event(), asyncio.Event()

    async def copied(context: MeivaContext) -> None:
        child_ready.set()
        await release_child.wait()
        assert context.original_incarnation is incarnation
        nested = replace(context)
        assert nested.original_incarnation is incarnation

    copied_task = asyncio.create_task(copied(replace(original)))
    await child_ready.wait()
    replacement = replace(original, original_incarnation=None)
    assert replacement.original_incarnation is None
    assert original.original_incarnation is incarnation
    release_child.set()
    await copied_task
