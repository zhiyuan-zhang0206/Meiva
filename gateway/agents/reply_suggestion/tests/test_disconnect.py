# meiva:note
# Stopping a suggestion's work when the client goes away.

from __future__ import annotations

import asyncio

import psycopg
import pytest

from base.db import Database
from base.lm.catalog import ModelCatalog
from gateway.agents.reply_suggestion.tests.support import DraftChat, GateServer, Harness
from gateway.http.disconnect import run_until_disconnect


async def test_the_work_result_is_returned_while_the_client_stays() -> None:
    async def work() -> str:
        await asyncio.sleep(0.01)
        return "done"

    async def connected() -> bool:
        return False

    assert await run_until_disconnect(work(), connected) == "done"


async def test_the_work_exception_reaches_a_connected_client() -> None:
    async def work() -> str:
        raise ValueError("boom")

    async def connected() -> bool:
        return False

    with pytest.raises(ValueError, match="boom"):
        await run_until_disconnect(work(), connected)


async def test_work_that_fails_while_the_disconnect_check_absorbs_cancellation_still_ends() -> None:
    async def absorbing() -> bool:
        try:
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            return False
        return False

    async def work() -> str:
        raise ValueError("boom")

    request = asyncio.ensure_future(run_until_disconnect(work(), absorbing))
    done, _pending = await asyncio.wait({request}, timeout=5)

    assert done, "the failed work left the request open"
    with pytest.raises(ValueError, match="boom"):
        request.result()


async def test_a_disconnect_cancels_the_work() -> None:
    cancelled = asyncio.Event()

    async def work() -> str:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "never"

    async def gone() -> bool:
        return True

    assert await run_until_disconnect(work(), gone) is None
    assert cancelled.is_set()


async def test_a_disconnect_stops_a_draft_that_is_in_flight(
    db_conn: psycopg.Connection, database: Database, model_catalog: ModelCatalog
) -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()
    chat = DraftChat(started=started, cancelled=cancelled)
    harness = Harness(db_conn, database, model_catalog, gate=GateServer(0.9), chat=chat)

    async def disconnects_once_drafting() -> bool:
        await started.wait()
        return True

    result = await run_until_disconnect(harness.suggest(), disconnects_once_drafting)

    assert result is None
    assert cancelled.is_set()
