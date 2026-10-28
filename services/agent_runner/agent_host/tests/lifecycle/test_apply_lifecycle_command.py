import asyncio
import contextlib
import tempfile
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.ownership.tests.ownership_support import (
    BusTap,
    PublicationDefectBus,
    admit_owner,
    agent_row,
    insert_lifecycle_command,
)
from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.config import settings
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus, EventBusConfig
from base.log.tests.log_records import LogRecords
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.paths import chrome_mcp_socket, meiva_home
from services.agent_runner.agent_host.lifecycle.shells import (
    apply_lifecycle_command,
    reap_and_announce_corpses,
)


async def _claimed_terminate(
    conn: psycopg.Connection,
    pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    *,
    kill_shells: bool,
) -> RuntimeIncarnation:
    agent_id = agent_row(conn)
    owner = await admit_owner(pool, agent_id, database_gate=database_gate)
    insert_lifecycle_command(
        conn, agent_id, "terminate", {"kill_all_shell_sessions": True} if kill_shells else None
    )
    conn.execute("UPDATE agents_meta SET session_index=7 WHERE id=%s", (agent_id,))
    conn.commit()
    await claim_inbound_batch(pool, agent_id, incarnation=owner)
    return owner


def _shell_cleanup_attempts(records: LogRecords, agent_id: int) -> list[str]:
    return [m for m in records.messages() if f"below 7 of agent {agent_id}" in m]


def _browser_release_attempts(records: LogRecords, agent_id: int) -> list[str]:
    return [m for m in records.messages("DEBUG") if f"agent {agent_id} browser-page release" in m]


async def test_a_terminate_that_asked_for_shells_cleans_below_its_cutoff_after_the_commit(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    bus_tap: BusTap,
    loguru_records: LogRecords,
    *,
    database_gate: ProcessDbGate,
) -> None:
    owner = await _claimed_terminate(db_conn, aops_pool, database_gate, kill_shells=True)
    await bus_tap.agent_updated_ids()

    kind = await apply_lifecycle_command(
        aops_pool, owner, bus=event_bus, resume_turn=False, home=meiva_home()
    )

    assert kind == "terminate"
    assert await bus_tap.agent_updated_ids() == [owner.agent_id]
    assert len(_browser_release_attempts(loguru_records, owner.agent_id)) == 1
    assert len(_shell_cleanup_attempts(loguru_records, owner.agent_id)) == 1


async def test_a_terminate_that_did_not_ask_for_shells_leaves_them_alone(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    loguru_records: LogRecords,
    *,
    database_gate: ProcessDbGate,
) -> None:
    owner = await _claimed_terminate(db_conn, aops_pool, database_gate, kill_shells=False)

    kind = await apply_lifecycle_command(
        aops_pool, owner, bus=event_bus, resume_turn=False, home=meiva_home()
    )

    assert kind == "terminate"
    assert len(_browser_release_attempts(loguru_records, owner.agent_id)) == 1
    assert _shell_cleanup_attempts(loguru_records, owner.agent_id) == []


async def test_a_restart_owes_no_cleanup(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    loguru_records: LogRecords,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    insert_lifecycle_command(db_conn, agent_id, "restart")
    await claim_inbound_batch(aops_pool, agent_id, incarnation=owner)

    kind = await apply_lifecycle_command(
        aops_pool, owner, bus=event_bus, resume_turn=False, home=meiva_home()
    )

    assert kind == "restart"
    assert _browser_release_attempts(loguru_records, agent_id) == []


async def test_nothing_claimed_applies_and_cleans_nothing(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    loguru_records: LogRecords,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)

    assert (
        await apply_lifecycle_command(
            aops_pool, owner, bus=event_bus, resume_turn=False, home=meiva_home()
        )
        is None
    )
    assert _browser_release_attempts(loguru_records, agent_id) == []


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("unknown event bus defect"), asyncio.CancelledError("publication cancelled")],
    ids=["error", "cancelled"],
)
async def test_a_publication_failure_still_releases_pages_and_cleans_shells_and_is_raised(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    loguru_records: LogRecords,
    failure: BaseException,
    *,
    database_gate: ProcessDbGate,
) -> None:
    owner = await _claimed_terminate(db_conn, aops_pool, database_gate, kill_shells=True)

    with pytest.raises(type(failure)) as caught:
        await apply_lifecycle_command(
            aops_pool,
            owner,
            bus=PublicationDefectBus(failure),
            resume_turn=False,
            home=meiva_home(),
        )

    assert caught.value is failure
    assert len(_browser_release_attempts(loguru_records, owner.agent_id)) == 1
    assert len(_shell_cleanup_attempts(loguru_records, owner.agent_id)) == 1
    assert db_conn.execute(
        "SELECT status FROM agents_meta WHERE id=%s", (owner.agent_id,)
    ).fetchone() == ("terminated",)


# meiva:note
# A Redis listener that accepts a PUBLISH write and never replies, like a stuck subscriber
# channel: `asked` is set once the publication bytes arrive, so the test can cancel the
# termination while it is waiting.
@asynccontextmanager
async def hung_redis_publication() -> AsyncGenerator[tuple[EventBus, asyncio.Event]]:
    asked = asyncio.Event()
    release = asyncio.Event()
    writers: list[asyncio.StreamWriter] = []

    async def hold(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writers.append(writer)
        try:
            with contextlib.suppress(ConnectionError):
                await reader.read(1)
                asked.set()
                await release.wait()
        finally:
            writer.close()

    server = await asyncio.start_server(hold, host="127.0.0.1", port=0)
    try:
        port = server.sockets[0].getsockname()[1]
        bus = EventBus(
            EventBusConfig(
                redis_url=f"redis://127.0.0.1:{port}",
                events_channel="test",
                publish_attempt_timeout_s=60.0,
            ),
            rng=seeded_rng(),
            timers=SYSTEM_TIMERS,
        )
        yield bus, asked
    finally:
        release.set()
        for writer in writers:
            writer.close()
        server.close()
        await server.wait_closed()


async def test_a_cancelled_publication_still_releases_pages_and_cleans_shells(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    loguru_records: LogRecords,
    *,
    database_gate: ProcessDbGate,
) -> None:
    _ = event_bus
    owner = await _claimed_terminate(db_conn, aops_pool, database_gate, kill_shells=True)
    async with hung_redis_publication() as (bus, asked):
        task = asyncio.create_task(
            apply_lifecycle_command(aops_pool, owner, bus=bus, resume_turn=False, home=meiva_home())
        )
        await asyncio.wait_for(asked.wait(), 5)
        task.cancel("publication wait cancelled")
        with pytest.raises(asyncio.CancelledError) as caught:
            await asyncio.wait_for(task, 5)

    assert len(_shell_cleanup_attempts(loguru_records, owner.agent_id)) == 1
    assert [m for m in loguru_records.messages() if "browser-page release" in m]
    assert getattr(caught.value, "__notes__", []) == []


# meiva:note
# A home whose browser-service socket path fits AF_UNIX (about 104 bytes; pytest's tmp_path is
# longer), so the release dials a real listener.
@pytest.fixture
def short_home() -> Generator[Path]:
    with tempfile.TemporaryDirectory(prefix="meiva-lc-", dir="/tmp") as name:
        yield Path(name)


# meiva:note
# The home's browser service as a real unix socket that takes the release request and never
# answers, so the release genuinely blocks until the caller is cancelled. `asked` is set once
# the request arrived.
@asynccontextmanager
async def hung_browser_service(home: Path) -> AsyncGenerator[asyncio.Event]:
    path = chrome_mcp_socket(home, settings.services.browser_cdp_port)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    asked = asyncio.Event()

    async def hold(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readline()
            asked.set()
            await reader.read()
        finally:
            writer.close()

    server = await asyncio.start_unix_server(hold, path=str(path))
    try:
        yield asked
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    "publication",
    [None, RuntimeError("publication failed"), asyncio.CancelledError("publication cancelled")],
    ids=["published", "error", "cancelled"],
)
async def test_a_cancelled_browser_release_still_cleans_shells_and_raises_the_first_failure(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    loguru_records: LogRecords,
    short_home: Path,
    publication: BaseException | None,
    *,
    database_gate: ProcessDbGate,
) -> None:
    owner = await _claimed_terminate(db_conn, aops_pool, database_gate, kill_shells=True)
    bus = event_bus if publication is None else PublicationDefectBus(publication)

    async with hung_browser_service(short_home) as asked:
        task = asyncio.create_task(
            apply_lifecycle_command(aops_pool, owner, bus=bus, resume_turn=False, home=short_home)
        )
        await asyncio.wait_for(asked.wait(), 5)
        task.cancel("browser wait cancelled")
        expected = asyncio.CancelledError if publication is None else type(publication)
        with pytest.raises(expected) as caught:
            await asyncio.wait_for(task, 5)

    assert len(_shell_cleanup_attempts(loguru_records, owner.agent_id)) == 1
    notes = getattr(caught.value, "__notes__", [])
    if publication is None:
        assert notes == []
    else:
        assert caught.value is publication
        assert [note.split(":")[0] for note in notes] == ["Post-commit browser cleanup also failed"]
        assert [m for m in loguru_records.messages("ERROR") if "browser cleanup also failed" in m]


async def test_a_reaped_corpse_is_announced_and_its_pages_released(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    event_bus: EventBus,
    bus_tap: BusTap,
    loguru_records: LogRecords,
    *,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    db_conn.execute(
        "UPDATE agents_meta SET status='idling', runtime_generation=NULL, runtime_owner=NULL, "
        "last_turn_fatal_at = now() - interval '1 hour' WHERE id=%s",
        (owner.agent_id,),
    )
    db_conn.commit()
    await bus_tap.agent_updated_ids()

    reaped = await reap_and_announce_corpses(
        aops_pool, "claim-test", bus=event_bus, home=meiva_home()
    )

    assert [corpse.agent_id for corpse in reaped] == [agent_id]
    assert await bus_tap.agent_updated_ids() == [agent_id]
    assert len(_browser_release_attempts(loguru_records, agent_id)) == 1


async def test_a_publication_defect_does_not_stop_the_next_corpse_from_being_released(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    loguru_records: LogRecords,
) -> None:
    corpses = [agent_row(db_conn), agent_row(db_conn)]
    db_conn.execute(
        "UPDATE agents_meta SET status='idling', last_turn_fatal_at = now() - interval '1 hour' "
        "WHERE id = ANY(%s)",
        (corpses,),
    )
    db_conn.commit()

    reaped = await reap_and_announce_corpses(
        aops_pool,
        "claim-test",
        bus=PublicationDefectBus(RuntimeError("unknown bus defect")),
        home=meiva_home(),
    )

    assert sorted(corpse.agent_id for corpse in reaped) == sorted(corpses)
    assert [
        r["extra"]["agent_id"] for r in loguru_records.events("corpse_reaper_publish_failed")
    ] == [corpse.agent_id for corpse in reaped]
    for corpse in reaped:
        assert len(_browser_release_attempts(loguru_records, corpse.agent_id)) == 1
