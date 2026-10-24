# meiva:note
# Stopping a suggestion's work when the client goes away.

from __future__ import annotations

import asyncio
from pathlib import Path

import psycopg
import pytest

from base.db import Database
from base.lm.catalog import ModelCatalog
from base.packages.tests.model_catalog import inert_keys
from gateway.agents.reply_suggestion.tests.provider_wire import DraftReply, GateReply, ProviderWire
from gateway.agents.reply_suggestion.tests.support import Harness
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
    db_conn: psycopg.Connection, database: Database, model_catalog: ModelCatalog, tmp_path: Path
) -> None:
    with ProviderWire(
        tmp_path / "provider", (GateReply(),), DraftReply(wait_for_disconnect=True)
    ) as wire:
        harness = Harness(
            db_conn, database, model_catalog, wire=wire, keys=inert_keys(model_catalog)
        )

        async def disconnects_once_drafting() -> bool:
            assert await asyncio.to_thread(wire.started.wait, 2), "draft never reached the provider"
            return True

        result = await asyncio.wait_for(
            run_until_disconnect(harness.suggest(), disconnects_once_drafting), 5
        )
        assert result is None
        assert await asyncio.to_thread(wire.disconnected.wait, 2), "draft socket stayed open"
