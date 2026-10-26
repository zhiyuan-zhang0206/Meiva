# meiva:note
# The cluster's own Redis, started for real: always authenticated, secret in a 0600 conf.
#
# `start_redis` is the shell of `redis_plan`; these tests run it against the host's real
# `redis-server` under the test's own home. The command line, the conf text and the step order it
# executes are asserted in `test_redis_plan.py`; here the daemon's behavior is the subject.

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import psutil
import pytest
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from base.clock import SYSTEM_TIMERS
from base.cluster import port_preflight
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.deploy.tests.containers import redis_server
from cli.commands.data_plane import cluster_instance as instance
from cli.commands.data_plane.cluster_instance import StartTuning
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.instance.redis_plan import redis_conf_text
from cli.commands.data_plane.tests.data_plane_records import with_redis_admin
from cli.commands.data_plane.tests.native_cluster import (
    ADMIN_PASSWORD,
    NATIVE_TEST_MACOS,
    RUNTIME_PASSWORD,
    NativeCluster,
    redis_command,
    redis_from_url,
    redis_ping,
)
from cli.commands.data_plane.tests.native_cluster import (
    native_cluster as native_cluster,
)
from cli.commands.data_plane.tests.native_cluster import (
    retained_children as retained_children,
)
from cli.database.tests.command_inputs import command_machine as command_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime

_UNASSIGNED = "192.0.2.1"


def _start(
    cluster: NativeCluster,
    machine: MachineIdentityReader,
    *,
    plane: DataPlaneConfig | None = None,
    runtime: str = RUNTIME_PASSWORD,
    bind_wait_s: float = 0.0,
) -> int:
    return instance.start_redis(
        plane or cluster.plane,
        cluster.redis_port,
        runtime,
        cluster.identity,
        home=cluster.home,
        machine=machine,
        tuning=StartTuning(bind_wait_s=bind_wait_s),
        macos=NATIVE_TEST_MACOS,
        timers=SYSTEM_TIMERS,
    )


def test_a_fresh_start_serves_only_authenticated_clients(
    native_cluster: NativeCluster, command_machine: MachineIdentityReader
) -> None:
    assert _start(native_cluster, command_machine) == 0
    port = native_cluster.redis_port
    with (
        redis.Redis(port=port, retry=Retry(NoBackoff(), 0)) as anonymous,
        pytest.raises(redis.AuthenticationError),
    ):
        redis_ping(anonymous)
    with redis.Redis(port=port, password=ADMIN_PASSWORD) as admin:
        assert redis_ping(admin)
    with redis.Redis(
        port=port, username=native_cluster.identity, password=RUNTIME_PASSWORD
    ) as runtime:
        assert redis_ping(runtime)


def test_the_admin_password_lives_in_a_private_conf_and_persistence_is_scheduled(
    native_cluster: NativeCluster, command_machine: MachineIdentityReader
) -> None:
    assert _start(native_cluster, command_machine) == 0
    conf = native_cluster.home / "redis" / "redis.conf"
    assert conf.stat().st_mode & 0o777 == 0o600
    assert conf.read_text() == redis_conf_text(ADMIN_PASSWORD)
    with redis.Redis(
        port=native_cluster.redis_port, password=ADMIN_PASSWORD, decode_responses=True
    ) as admin:
        assert redis_command(admin, "CONFIG", "GET", "save") == {"save": "900 1 300 10 60 10000"}
        directory = redis_command(admin, "CONFIG", "GET", "dir")
        assert directory == {"dir": str((native_cluster.home / "redis").resolve())}


# meiva:note
# A journal retry repairs the config after an old-password false-down probe.
def test_a_running_redis_repairs_a_stale_conf_and_stays_up(
    native_cluster: NativeCluster, command_machine: MachineIdentityReader
) -> None:
    assert _start(native_cluster, command_machine) == 0
    conf = native_cluster.home / "redis" / "redis.conf"
    conf.write_text('requirepass "stale-old-password"\n')

    assert _start(native_cluster, command_machine) == 0

    assert conf.read_text() == redis_conf_text(ADMIN_PASSWORD)
    with redis.Redis(port=native_cluster.redis_port, password=ADMIN_PASSWORD) as admin:
        assert redis_ping(admin)


@pytest.mark.parametrize(
    ("admin", "runtime"), [("", ""), ("", RUNTIME_PASSWORD), (ADMIN_PASSWORD, "")]
)
def test_missing_credentials_refuse_before_any_effect(
    native_cluster: NativeCluster, command_machine: MachineIdentityReader, admin: str, runtime: str
) -> None:
    plane = dataclasses.replace(native_cluster.plane, redis_admin_password=admin)

    with pytest.raises(ValueError, match="password"):
        _start(native_cluster, command_machine, plane=plane, runtime=runtime)

    assert not (native_cluster.home / "redis").exists()
    assert port_preflight.strict_listeners_on(native_cluster.redis_port) == []


# meiva:note
# The boot pass may have delivered the gateway login, the generation and the API token to this
# process (the test home is in its environment as `MEIVA_HOME`); the daemon it spawns keeps none of
# them.
def test_the_daemon_inherits_no_meiva_authority(
    native_cluster: NativeCluster, command_machine: MachineIdentityReader
) -> None:
    assert _start(native_cluster, command_machine) == 0
    with redis.Redis(
        port=native_cluster.redis_port, password=ADMIN_PASSWORD, decode_responses=True
    ) as admin:
        info = redis_command(admin, "INFO", "server")
    assert isinstance(info, dict)
    inherited = psutil.Process(int(cast("dict[str, str]", info)["process_id"])).environ()
    assert not [name for name in inherited if name.startswith("MEIVA_")]


@pytest.mark.requires_host("linux")
def test_linux_redis_refuses_to_start_when_the_declared_reach_never_appears(
    native_cluster: NativeCluster,
    command_machine: ConfiguredMachineIdentity,
    capsys: pytest.CaptureFixture[str],
) -> None:
    command_machine.set(host=_UNASSIGNED)

    assert _start(native_cluster, command_machine, bind_wait_s=0.0) == 1

    assert "not assigned to any local interface" in capsys.readouterr().err
    assert not (native_cluster.home / "redis" / "redis.conf").exists()
    assert port_preflight.strict_listeners_on(native_cluster.redis_port) == []


# meiva:note
# A foreign Redis that even accepts this home's admin password is not ours: native ownership refuses
# before any ACL or config effect.
def test_a_foreign_redis_keeps_its_acl_and_config(
    redis_access: None,
    tmp_path: Path,
    command_machine: MachineIdentityReader,
    data_plane_config: DataPlaneConfig,
) -> None:
    config = tmp_path / "redis" / "redis.conf"
    config.parent.mkdir()
    config.write_text("original config\n")
    plane = with_redis_admin(
        dataclasses.replace(data_plane_config, redis_url="redis://unowned:x@127.0.0.1:1/0"), "admin"
    )
    with redis_server() as url:
        port = urlsplit(url).port
        assert port is not None
        with redis_from_url(url, decode_responses=True) as client:
            redis_command(client, "CONFIG", "SET", "requirepass", "admin")
            before = redis_command(client, "ACL", "LIST")
            rc = instance.start_redis(
                plane,
                port,
                "runtime",
                "unowned",
                home=tmp_path,
                machine=command_machine,
                tuning=StartTuning(bind_wait_s=0.0),
                macos=NATIVE_TEST_MACOS,
                timers=SYSTEM_TIMERS,
            )
            assert rc == 1
            assert redis_command(client, "ACL", "LIST") == before
    assert config.read_text() == "original config\n"
