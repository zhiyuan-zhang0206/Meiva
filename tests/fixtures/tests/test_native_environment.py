import os
from pathlib import Path

import psycopg
import redis
import redis.asyncio
from psycopg.conninfo import conninfo_to_dict

from base.config import ConfigBoot, settings
from tests.fixtures.env_bootstrap import UNPROVISIONED_REDIS_URL


def test_native_process_environment_provisions_isolated_data_plane(
    process_config: ConfigBoot,
    postgres_access: object,
    db_url: str,
    redis_access: object,
    redis_url: str,
) -> None:
    assert Path(os.environ["MEIVA_HOME"]) != Path.home() / ".meiva"
    assert not settings.general.os_jobs_enabled
    assert db_url == settings.data_plane.db_url
    assert redis_url == settings.data_plane.redis_url
    db = conninfo_to_dict(settings.data_plane.db_url)
    assert db["dbname"] != "unprovisioned"
    assert settings.data_plane.redis_url != UNPROVISIONED_REDIS_URL
    expected_override = process_config.view.data_plane.pg_throwaway_base.strip()
    if expected_override:
        with psycopg.connect(settings.data_plane.db_url) as connection:
            row = connection.execute("SHOW data_directory").fetchone()
        assert row is not None
        assert Path(row[0]).parents[1] == Path(expected_override).absolute()


def test_native_sync_data_plane_calls_reach_provisioned_services(
    postgres_access: object,
    db_url: str,
    redis_access: object,
    redis_url: str,
) -> None:
    with psycopg.connect(db_url) as connection:
        assert connection.execute("SELECT 1").fetchone() == (1,)
    with redis.Redis.from_url(redis_url) as client:
        assert client.ping()


async def test_native_async_data_plane_calls_reach_provisioned_services(
    postgres_access: object,
    db_url: str,
    redis_access: object,
    redis_url: str,
) -> None:
    async with await psycopg.AsyncConnection.connect(db_url) as connection:
        assert await (await connection.execute("SELECT 1")).fetchone() == (1,)
    async with redis.asyncio.Redis.from_url(redis_url) as client:
        assert await client.ping()
