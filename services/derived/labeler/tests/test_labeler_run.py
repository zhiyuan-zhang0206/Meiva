from __future__ import annotations

import asyncio
from pathlib import Path

import psycopg
import pytest

from base.config import ConfigBoot
from base.daemon.endpoints import ServiceEndpoints
from base.db import Database
from base.native_process.loaded_commit import LoadedCommit
from services.derived.labeler import daemon
from services.derived.labeler.tests.db_faults import table_missing
from services.derived.labeler.tests.service_probe import assert_released, our_backends, wait_serving


async def test_run_serves_while_it_polls_and_releases_everything_when_stopped(
    db_conn: psycopg.Connection, database: Database, *, process_config: ConfigBoot, tmp_path: Path
) -> None:
    runtime = tmp_path / "run"
    pidfile = runtime / "labeler.pid"
    port = ServiceEndpoints.from_boot(process_config, runtime=runtime).of("labeler").health_port
    service = asyncio.create_task(
        daemon.run(
            process_config=process_config,
            runtime=runtime,
            database=lambda: database,
            image=LoadedCommit(Path(), None),
        )
    )
    try:
        await wait_serving(port, pidfile)
        assert our_backends(db_conn) > 0
    finally:
        service.cancel()
        await asyncio.gather(service, return_exceptions=True)

    assert service.cancelled()
    await assert_released(db_conn, pidfile, port)


async def test_run_releases_everything_when_the_poll_fails(
    db_conn: psycopg.Connection, database: Database, *, process_config: ConfigBoot, tmp_path: Path
) -> None:
    runtime = tmp_path / "run"
    pidfile = runtime / "labeler.pid"
    port = ServiceEndpoints.from_boot(process_config, runtime=runtime).of("labeler").health_port

    with table_missing(db_conn, "agents"), pytest.raises(psycopg.errors.UndefinedTable):
        await asyncio.wait_for(
            daemon.run(
                process_config=process_config,
                runtime=runtime,
                database=lambda: database,
                image=LoadedCommit(Path(), None),
            ),
            timeout=30.0,
        )

    await assert_released(db_conn, pidfile, port)
