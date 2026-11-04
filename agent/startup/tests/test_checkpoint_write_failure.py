from __future__ import annotations

import asyncio

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agent.startup import wrap_saver_writes_with_loud_failure
from base.log.tests.log_records import LogRecords


class _StubSaver(AsyncPostgresSaver):
    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def aput(
        self, config: RunnableConfig, *_args: object, **_kwargs: object
    ) -> RunnableConfig:
        raise self._error

    async def aput_writes(self, config: RunnableConfig, *_args: object, **_kwargs: object) -> None:
        raise self._error


# meiva:note
# The `extra` mapping of every record the loud-failure wrapper logged, read through the fixture's own
# reader instead of indexing a record dict whose values are `Any`.
def _write_failure_events(records: LogRecords) -> list[dict[str, object]]:
    return records.extras(event="checkpoint_write_failed")


_CONFIG: RunnableConfig = {"configurable": {"thread_id": "7"}}


async def test_cancellation_passes_through_without_a_write_failure_event(
    loguru_records: LogRecords,
) -> None:
    saver = _StubSaver(asyncio.CancelledError())
    wrap_saver_writes_with_loud_failure(saver)
    with pytest.raises(asyncio.CancelledError):
        await saver.aput(_CONFIG, None, None, None)
    with pytest.raises(asyncio.CancelledError):
        await saver.aput_writes(_CONFIG, None, None)
    assert _write_failure_events(loguru_records) == []


async def test_real_failures_still_log(
    loguru_records: LogRecords,
) -> None:
    saver = _StubSaver(RuntimeError("write failed"))
    wrap_saver_writes_with_loud_failure(saver)
    with pytest.raises(RuntimeError):
        await saver.aput(_CONFIG, None, None, None)
    with pytest.raises(RuntimeError):
        await saver.aput_writes(_CONFIG, None, None)
    assert [event["method"] for event in _write_failure_events(loguru_records)] == [
        "aput",
        "aput_writes",
    ]
