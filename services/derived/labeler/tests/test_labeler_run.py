from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import psycopg
import pytest

from base.clock import SYSTEM_TIMERS
from base.config import ConfigBoot
from base.daemon.endpoints import ServiceEndpoints
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import meiva_home
from services.derived.labeler import daemon
from services.derived.labeler.tests.db_faults import table_missing
from services.derived.labeler.tests.service_probe import assert_released, our_backends, wait_serving


@pytest.fixture
def database_gate() -> ProcessDbGate:
    version = CodeVersion(LoadedCommit.capture())
    return ProcessDbGate(version=version.get, process=f"lr-{uuid.uuid4().hex}")


@pytest.fixture
def observer(db_conn: psycopg.Connection) -> psycopg.Connection:
    # meiva:note
    # The observer stays connected as an unrelated Meiva client throughout shutdown. Counting
    # every meiva:* backend would count this observer itself and falsely report a service leak.
    db_conn.execute("SET application_name = 'meiva:labeler-observer'")
    db_conn.commit()
    return db_conn


async def test_run_serves_while_it_polls_and_releases_everything_when_stopped(
    observer: psycopg.Connection,
    database: Database,
    database_gate: ProcessDbGate,
    *,
    process_config: ConfigBoot,
    tmp_path: Path,
) -> None:
    application_name = database_gate.application_name()
    runtime = tmp_path / "run"
    pidfile = runtime / "labeler.pid"
    port = ServiceEndpoints.from_boot(process_config, runtime=runtime).of("labeler").health_port
    service = asyncio.create_task(
        daemon.run(
            process_config=process_config,
            runtime=runtime,
            database=lambda: database,
            image=LoadedCommit(Path(), None),
            home=meiva_home(),
            timers=SYSTEM_TIMERS,
        )
    )
    try:
        await wait_serving(port, pidfile)
        assert our_backends(observer, application_name=application_name) > 0
    finally:
        service.cancel()
        await asyncio.gather(service, return_exceptions=True)

    assert service.cancelled()
    await assert_released(observer, pidfile, port, application_name=application_name)
    assert observer.execute("SHOW application_name").fetchone() == ("meiva:labeler-observer",)


async def test_run_releases_everything_when_the_poll_fails(
    observer: psycopg.Connection,
    database: Database,
    database_gate: ProcessDbGate,
    *,
    process_config: ConfigBoot,
    tmp_path: Path,
) -> None:
    application_name = database_gate.application_name()
    runtime = tmp_path / "run"
    pidfile = runtime / "labeler.pid"
    port = ServiceEndpoints.from_boot(process_config, runtime=runtime).of("labeler").health_port

    with table_missing(observer, "agents"), pytest.raises(psycopg.errors.UndefinedTable):
        await asyncio.wait_for(
            daemon.run(
                process_config=process_config,
                runtime=runtime,
                database=lambda: database,
                image=LoadedCommit(Path(), None),
                home=meiva_home(),
                timers=SYSTEM_TIMERS,
            ),
            timeout=30.0,
        )

    await assert_released(observer, pidfile, port, application_name=application_name)
    assert observer.execute("SHOW application_name").fetchone() == ("meiva:labeler-observer",)


async def test_release_observer_refuses_a_live_owned_backend(
    observer: psycopg.Connection,
    database: Database,
    database_gate: ProcessDbGate,
    *,
    process_config: ConfigBoot,
    tmp_path: Path,
) -> None:
    application_name = database_gate.application_name()
    runtime = tmp_path / "run"
    port = ServiceEndpoints.from_boot(process_config, runtime=runtime).of("labeler").health_port
    pidfile = runtime / "labeler.pid"
    with database.connect(autocommit=True) as held:
        assert our_backends(observer, application_name=application_name) == 1
        with pytest.raises(
            pytest.fail.Exception, match="the service left its pool open"
        ) as failure:
            await assert_released(
                observer, pidfile, port, application_name=application_name, timeout_s=0.0
            )
        assert str(held.info.backend_pid) in str(failure.value)
        assert application_name in str(failure.value)
    await assert_released(observer, pidfile, port, application_name=application_name)
    assert observer.execute("SHOW application_name").fetchone() == ("meiva:labeler-observer",)
