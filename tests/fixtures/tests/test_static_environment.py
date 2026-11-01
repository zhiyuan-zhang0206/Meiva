import asyncio
import os
import socket
from pathlib import Path

import psycopg
import pytest
import redis
import redis.asyncio
from psycopg.conninfo import conninfo_to_dict

from base.config import ConfigBoot, settings
from tests.fixtures.env_bootstrap import UNPROVISIONED_DB_URL, UNPROVISIONED_REDIS_URL
from tests.fixtures.static_environment import static_mode


def test_unprovisioned_process_keeps_its_home_and_sentinel_urls(
    process_config: ConfigBoot,
) -> None:
    assert Path(os.environ["MEIVA_HOME"]) != Path.home() / ".meiva"
    assert not settings.general.os_jobs_enabled
    assert process_config.view.general.os_jobs_enabled is False
    db = conninfo_to_dict(settings.data_plane.db_url)
    expected = conninfo_to_dict(UNPROVISIONED_DB_URL)
    assert {key: db[key] for key in expected} == expected
    assert settings.data_plane.redis_url == UNPROVISIONED_REDIS_URL


def test_sync_data_plane_calls_are_refused_without_native_provisioning(
    pytestconfig: pytest.Config,
    process_config: object,
) -> None:
    expected = "Static tests cannot use" if static_mode(pytestconfig) else "Postgres"
    redis_expected = "Static tests cannot use" if static_mode(pytestconfig) else "Redis"
    with pytest.raises(pytest.fail.Exception, match=expected):
        psycopg.connect(settings.data_plane.db_url)
    with pytest.raises(pytest.fail.Exception, match=expected):
        psycopg.Connection.connect(settings.data_plane.db_url)
    with redis.Redis.from_url(settings.data_plane.redis_url) as client:
        with pytest.raises(pytest.fail.Exception, match=redis_expected):
            client.ping()
        with pytest.raises(pytest.fail.Exception, match=redis_expected):
            pipeline = client.pipeline()
            pipeline.ping()
            pipeline.execute()


async def test_async_data_plane_calls_are_refused_without_native_provisioning(
    pytestconfig: pytest.Config,
    process_config: object,
) -> None:
    expected = "Static tests cannot use" if static_mode(pytestconfig) else "Postgres"
    redis_expected = "Static tests cannot use" if static_mode(pytestconfig) else "Redis"
    with pytest.raises(pytest.fail.Exception, match=expected):
        await psycopg.AsyncConnection.connect(settings.data_plane.db_url)
    async with redis.asyncio.Redis.from_url(settings.data_plane.redis_url) as client:
        with pytest.raises(pytest.fail.Exception, match=redis_expected):
            await client.ping()
        with pytest.raises(pytest.fail.Exception, match=redis_expected):
            pipeline = client.pipeline()
            pipeline.ping()
            await pipeline.execute()


async def test_external_network_calls_fail_closed_in_static_mode(
    pytestconfig: pytest.Config,
) -> None:
    if static_mode(pytestconfig):
        with pytest.raises(
            pytest.fail.Exception, match="Static tests cannot make external network calls"
        ):
            socket.getaddrinfo("example.com", 80)
        with pytest.raises(
            pytest.fail.Exception, match="Static tests cannot make external network calls"
        ):
            socket.create_connection(("8.8.8.8", 53), timeout=1)
        with (
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s,
            pytest.raises(
                pytest.fail.Exception, match="Static tests cannot make external network calls"
            ),
        ):
            s.connect(("8.8.8.8", 53))
        with (
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s,
            pytest.raises(
                pytest.fail.Exception, match="Static tests cannot make external network calls"
            ),
        ):
            s.connect_ex(("8.8.8.8", 53))

        # meiva:note
        # Pre-syscall tuple unpacking and IDNA label validation match stdlib socket semantics.
        with pytest.raises(UnicodeError):
            socket.getaddrinfo("a" * 64 + ".example", 80)

        # meiva:note
        # Local socketpair and loopback communication remain available in static mode.
        assert socket.getaddrinfo("localhost", 80)
        assert socket.getaddrinfo(b"localhost", 80)
        p1, p2 = socket.socketpair()
        try:
            p1.sendall(b"ping")
            assert p2.recv(10) == b"ping"
        finally:
            p1.close()
            p2.close()

        # meiva:note
        # Asyncio event loop boots and executes cleanly under the network guard.
        await asyncio.sleep(0.001)
