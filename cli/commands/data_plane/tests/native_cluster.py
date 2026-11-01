# meiva:note
# A real per-cluster Postgres and Redis in the test's own home, and its teardown.
#
# The shell tests of the data-plane bring-up run the real `postgres` and `redis-server` of the
# host, on ports the kernel handed this test, under `unit_home`. `NativeCluster` carries what a
# start reads: the home, the two ports, the `DataPlaneConfig` whose URLs name them, and the
# children the CLI retains. The record, the start intent and the `.env` a born home carries are
# real files in the home, so `get_record`, `needs_provision` and the Redis password read them the
# way a start does.

from __future__ import annotations

import contextlib
import dataclasses
import json
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

import pytest
import redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError

from base import cluster
from base.cluster.dataplane.pg_tools import pg_tool
from base.host.env import port_lease
from base.paths import meiva_home
from cli.commands.data_plane.config import DataPlaneConfig

__all__ = [
    "ADMIN_PASSWORD",
    "BEARER",
    "NATIVE_TEST_MACOS",
    "NATIVE_TEST_SYSTEM",
    "RUNTIME_PASSWORD",
    "NativeCluster",
    "free_port",
    "native_cluster",
    "redis_command",
    "redis_from_url",
    "redis_ping",
    "retained_children",
    "shutdown_redis",
]

BEARER = "test_bearer_abc123"
ADMIN_PASSWORD = "test_redis_admin_abc123"  # noqa: S105
RUNTIME_PASSWORD = "test_redis_runtime_abc123"  # noqa: S105
_IDENTITY = "meiva_tinst"

# meiva:note
# The platform value these tests start and stop a cluster with. They run the test environment's
# own `redis-server` and `redis-cli`, found through PATH like the other throwaway servers of the
# suite, which is the non-macOS branch of binary resolution; the macOS branch (the Homebrew keg,
# loopback-only Redis) is asserted as values in `instance/test_redis_plan.py` and
# `instance/test_redis_launch_argv.py`.
NATIVE_TEST_MACOS = False

# meiva:note
# The operating system a launch receipt is written for: `postgres.start` takes it as a value, and a
# receipt that names another system is refused when it is read back. This suite drives the real
# server the way the Linux lane does, which is the same stand-in `NATIVE_TEST_MACOS` above names.
NATIVE_TEST_SYSTEM = "linux"


# meiva:note
# One more port this session holds, from the host-wide lease.
#
# It used to probe `:0` and close: the kernel then owed the number to nobody, so a
# sibling worker — or this very process's own session table, drawn from the same
# ephemeral pool at import — could hold it when a record was written, and the record
# validated as `duplicate start port reservation`.
def free_port() -> int:
    return port_lease.lease_ports(1)[0]


# meiva:note
# One born gateway home with its own pg and redis ports and the config that names them.
@dataclass(frozen=True)
class NativeCluster:
    home: Path
    ports: cluster.ClusterPorts
    plane: DataPlaneConfig
    retained_children: list[subprocess.Popen[bytes]]

    @property
    def pg_port(self) -> int:
        return self.ports["postgres"]

    @property
    def redis_port(self) -> int:
        return self.ports["redis"]

    @property
    def pooler_port(self) -> int:
        return self.ports["pgbouncer"]

    @property
    def identity(self) -> str:
        return _IDENTITY

    @property
    def pg_data(self) -> Path:
        return self.home / "pg"

    def record(self) -> cluster.ClusterRecord:
        return cluster.ClusterRecord(
            ports=self.ports.copy(),
            gateway_home=str(self.home),
            created_at="test",
        )

    # meiva:note
    # The start intent of a home `meiva init` has configured: its record and its phase. A first
    # start provisions it (`configured`); a home past that without a database authority ledger
    # is refused.
    def write_intent(self, phase: str = "configured") -> None:
        intent = {
            "version": 1,
            "home": str(self.home),
            "checkout": str(Path(__file__).resolve().parents[5]),
            "roles": ["agent-runner", "gateway"],
            "config_digest": None,
            "phase": phase,
            "record": asdict(self.record()),
            "env": {},
        }
        path = self.home / "start-intent.json"
        path.write_text(json.dumps(intent))
        path.chmod(0o600)

    # meiva:note
    # The `.env` a born gateway home carries: the credential-free database endpoint and the
    # Redis runtime password.
    def write_env(self) -> None:
        values = {
            "MEIVA_DB_URL": f"postgresql://{_IDENTITY}@127.0.0.1:{self.pg_port}/{_IDENTITY}",
            "MEIVA_REDIS_PASSWORD": RUNTIME_PASSWORD,
        }
        (self.home / ".env").write_text(
            "".join(f"{key}={value}\n" for key, value in values.items())
        )

    def born(self) -> NativeCluster:
        self.write_intent()
        self.write_env()
        return self


@pytest.fixture
def retained_children() -> list[subprocess.Popen[bytes]]:
    return []


@pytest.fixture
def native_cluster(
    unit_home: Path,
    data_plane_config: DataPlaneConfig,
    retained_children: list[subprocess.Popen[bytes]],
    postgres_access: None,
    redis_access: None,
    distinct_free_ports: Callable[[int], list[int]],
) -> Iterator[NativeCluster]:
    ports = cluster.new_home_ports()
    reserved = set(ports.values())
    for _ in range(8):
        native_ports = distinct_free_ports(3)
        if reserved.isdisjoint(native_ports):
            break
    else:
        raise RuntimeError(
            "could not allocate native cluster ports outside the home table in 8 attempts"
        )
    pg_port, redis_port, pooler_port = native_ports
    ports.update(postgres=pg_port, redis=redis_port, pgbouncer=pooler_port)
    plane = dataclasses.replace(
        data_plane_config,
        db_url=f"postgresql://{_IDENTITY}@127.0.0.1:{pg_port}/{_IDENTITY}",
        redis_url=f"redis://{_IDENTITY}:{RUNTIME_PASSWORD}@127.0.0.1:{redis_port}/0",
        redis_admin_password=ADMIN_PASSWORD,
        cluster_secret=BEARER,
        pgbouncer_enabled=False,
        events_channel="meiva:tinst:events",
        trusted_cidrs="",
        walg_config_file=None,
    )
    running = NativeCluster(unit_home, ports, plane, retained_children)
    try:
        yield running
    finally:
        subprocess.run(
            [
                str(pg_tool("pg_ctl", home=meiva_home())),
                "-D",
                str(running.pg_data),
                "-m",
                "immediate",
                "stop",
            ],
            check=False,
            capture_output=True,
        )
        for child in retained_children:
            child.wait(timeout=5)
        with contextlib.suppress(RedisError):
            shutdown_redis(redis_port, ADMIN_PASSWORD, save=False)


# meiva:note
# Ends a test cluster's real redis-server through its own protocol, saving the dataset or not. The
# server closes the connection as it exits, so a dropped connection is the success answer.
def shutdown_redis(port: int, password: str, *, save: bool) -> None:
    client = redis.Redis(host="127.0.0.1", port=port, password=password, socket_timeout=5)
    try:
        client.execute_command("SHUTDOWN", "SAVE" if save else "NOSAVE")  # pyright: ignore[reportUnknownMemberType]
    except RedisConnectionError:
        return
    finally:
        client.close()


# meiva:note
# redis-py's stubs leave the client's command methods partly unknown; the tests drive the client
# through these typed readings.
def redis_ping(client: redis.Redis) -> bool:
    return bool(client.ping())  # pyright: ignore[reportUnknownMemberType]


# meiva:note
# One raw command; the reply is whatever the client's response callback made of it.
def redis_command(client: redis.Redis, *args: str) -> object:
    return cast("object", client.execute_command(*args))  # pyright: ignore[reportUnknownMemberType]


# meiva:note
# A client for `url` (redis-py's `from_url` takes untyped keyword arguments).
def redis_from_url(url: str, *, decode_responses: bool = False) -> redis.Redis:
    return redis.Redis.from_url(url, decode_responses=decode_responses)  # pyright: ignore[reportUnknownMemberType]
