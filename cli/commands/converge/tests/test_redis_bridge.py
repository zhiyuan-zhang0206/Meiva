# meiva:note
# Retiring the standalone launchd Redis bridge, and probing the root-supervised relay.
#
# The plist and the installed relay copy are real files under `tmp_path`. `launchctl` is the one
# thing a Linux CI host cannot run, so the host's `run` is `FakeCommands`: it answers the three
# commands retirement issues (`print`, `bootout`, `print` again) and fails on any other command.

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import redis

from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.cluster.ports import new_home_ports
from base.config import ConfigBoot
from base.deploy.tests.containers import free_port, redis_server
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import (
    DARWIN_ARM64,
    LINUX_X86,
    FakeCommands,
    Reply,
    fake_host,
)
from base.native_process.os_platform import Platform
from base.sessions.tests.refuse_helper import refuse_helper
from base.telemetry import EventPipeline
from cli.commands.converge import redis_bridge as bridge
from cli.commands.converge.plans.bridge_status import not_required
from cli.commands.converge.spec import ConvergeCtx
from cli.database.runtime import CommandRuntime, UserSession
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.cli_tests import operator_pipeline as operator_pipeline
from cli.database.tests.command_inputs import command_runtime as command_runtime

_UID = 501
_LABEL = bridge.LABEL


@dataclass(frozen=True)
class BridgeFiles:
    home: Path
    user_home: Path
    installed: Path
    plist: Path


@pytest.fixture
def files(tmp_path: Path) -> BridgeFiles:
    """A home holding the installed relay copy and a user holding the LaunchAgent plist."""
    home, user_home = tmp_path / "home", tmp_path / "user"
    installed = home / "redis-bridge" / "relay.py"
    installed.parent.mkdir(parents=True)
    installed.write_text("# stale relay\n")
    plist = bridge.launchd_plist_path(user_home)
    plist.parent.mkdir(parents=True)
    plist.write_text("stale plist")
    return BridgeFiles(home, user_home, installed, plist)


# meiva:note
# A launchctl whose `print` answers `loaded` in turn and whose `bootout` succeeds.
def _launchd(*loaded: bool) -> FakeCommands:
    commands = FakeCommands()
    commands.on(
        ("launchctl", "print"),
        *(Reply(returncode=0 if state else 1) for state in loaded),
        repeat=len(loaded) == 1,
    )
    commands.on(("launchctl", "bootout"), Reply(returncode=0), repeat=True)
    return commands


def _mac(commands: FakeCommands) -> Host:
    return fake_host(commands, platform=DARWIN_ARM64, uid=_UID)


def _context(
    files: BridgeFiles,
    command_runtime: CommandRuntime,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    host: Host,
) -> ConvergeCtx:
    return ConvergeCtx(
        repo=files.home.parent / "repo",
        meiva_home=files.home,
        roles=None,
        config=command_runtime.config,
        machine=command_runtime.machine,
        database_factory=operator_database,
        producer=operator_pipeline,
        helper=refuse_helper,
        host=host,
        session=UserSession(user_home=files.user_home, login_shell="/bin/zsh", interactive=False),
        services=frozenset(),
        timers=command_runtime.timers,
    )


def test_converge_retires_the_standalone_launchd_job_and_installed_copy(
    files: BridgeFiles,
    command_runtime: CommandRuntime,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    commands = _launchd(True, False)
    ctx = _context(
        files, command_runtime, operator_database, operator_pipeline, host=_mac(commands)
    )

    bridge.retire_launchd_redis_bridge(ctx)

    target = f"gui/{_UID}/{_LABEL}"
    assert commands.argvs == [
        ("launchctl", "print", target),
        ("launchctl", "bootout", target),
        ("launchctl", "print", target),
    ]
    assert not files.plist.exists()
    assert not files.installed.exists()


def test_files_left_by_an_unloaded_job_are_removed_without_a_bootout(
    files: BridgeFiles,
) -> None:
    commands = _launchd(False)

    bridge.retire_launchd_bridge(files.home, host=_mac(commands), user_home=files.user_home)

    assert commands.argvs == [("launchctl", "print", f"gui/{_UID}/{_LABEL}")]
    assert not files.plist.exists()
    assert not files.installed.exists()


def test_a_home_with_nothing_of_the_standalone_bridge_is_left_alone(tmp_path: Path) -> None:
    commands = _launchd(False)

    bridge.retire_launchd_bridge(
        tmp_path / "home", host=_mac(commands), user_home=tmp_path / "user"
    )

    assert commands.argvs == [("launchctl", "print", f"gui/{_UID}/{_LABEL}")]


def test_retire_preserves_artifacts_when_launchd_job_does_not_exit(files: BridgeFiles) -> None:
    commands = _launchd(True)

    with pytest.raises(RuntimeError, match="remained loaded"):
        bridge.retire_launchd_bridge(
            files.home, host=_mac(commands), user_home=files.user_home, timeout_s=0.0
        )

    assert files.plist.exists()
    assert files.installed.exists()


def test_a_job_that_unloads_late_is_waited_for(files: BridgeFiles) -> None:
    commands = _launchd(True, True, True, False)

    bridge.retire_launchd_bridge(files.home, host=_mac(commands), user_home=files.user_home)

    assert [argv[1] for argv in commands.argvs] == ["print", "bootout", "print", "print", "print"]
    assert not files.plist.exists()


def test_a_linux_host_never_asks_launchd(
    files: BridgeFiles,
    command_runtime: CommandRuntime,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    commands = FakeCommands()
    host = fake_host(commands, platform=LINUX_X86, uid=_UID)
    ctx = _context(files, command_runtime, operator_database, operator_pipeline, host=host)

    bridge.retire_launchd_redis_bridge(ctx)

    assert commands.argvs == []
    assert files.plist.exists() and files.installed.exists()


# meiva:note
# redis-py's stubs leave `from_url` and `config_set` untyped, which is what the ignores accept.
def test_ping_answers_none_when_redis_accepts_the_urls_credentials(redis_access: None) -> None:
    with redis_server() as url, redis.Redis.from_url(url) as client:  # pyright: ignore[reportUnknownMemberType]
        client.config_set("requirepass", "bridge-pw")  # pyright: ignore[reportUnknownMemberType]
        address = urlsplit(url)
        host_port = f"{address.hostname}:{address.port}"

        assert bridge.ping_redis(f"redis://:bridge-pw@{host_port}/0") is None
        refused = bridge.ping_redis(f"redis://{host_port}/0")
        assert refused is not None and "authenticated" in refused


def test_ping_names_why_nothing_answered(redis_access: None) -> None:
    detail = bridge.ping_redis(f"redis://127.0.0.1:{free_port()}/0")

    assert detail is not None
    assert "refused" in detail.lower()


def test_an_unsupported_platform_does_not_read_the_relay_owner(tmp_path: Path) -> None:
    config = ConfigBoot()
    machine = ConfiguredMachineIdentity(config)

    status = bridge.probe_bridge_on("linux", tmp_path, machine=machine, config=config)

    assert not status.required
    assert not config.prepared


def test_a_remote_data_plane_has_no_local_relay(
    tmp_path: Path, command_runtime: CommandRuntime
) -> None:
    command_runtime.config.set_field("db_url", "postgresql://meiva@192.0.2.10:6433/meiva")
    command_runtime.config.set_field("redis_url", "redis://meiva:secret@127.0.0.1:6380/0")
    command_runtime.config.set_field("machine_host", "10.64.0.7")

    status = bridge.probe_bridge_on(
        "darwin", tmp_path, machine=command_runtime.machine, config=command_runtime.config
    )

    assert not status.required


def test_a_loopback_machine_needs_no_relay(tmp_path: Path, command_runtime: CommandRuntime) -> None:
    command_runtime.config.set_field("db_url", "postgresql://meiva@127.0.0.1:6433/meiva")
    command_runtime.config.set_field("redis_url", "redis://meiva:secret@127.0.0.1:6380/0")
    command_runtime.config.set_field("machine_host", "127.0.0.1")
    command_runtime.machine.reset()

    status = bridge.probe_bridge_on(
        "darwin", tmp_path, machine=command_runtime.machine, config=command_runtime.config
    )

    assert not status.required


def test_a_relay_without_a_registry_record_reports_it_unconfigured(
    tmp_path: Path, command_runtime: CommandRuntime
) -> None:
    command_runtime.config.set_field("cluster_secret", "cluster-secret")
    command_runtime.config.set_field("db_url", "postgresql://meiva@127.0.0.1:6433/meiva")
    command_runtime.config.set_field("redis_url", "redis://meiva:secret@127.0.0.1:6380/0")
    command_runtime.config.set_field("machine_host", "10.64.0.7")
    command_runtime.machine.reset()

    status = bridge.probe_bridge_on(
        "darwin", tmp_path, machine=command_runtime.machine, config=command_runtime.config
    )

    assert status.required
    assert not status.serving
    assert status.endpoint == ""
    assert status.detail == f"bridge unconfigured: no registry record for {tmp_path}"


def test_a_relay_that_does_not_answer_reports_its_recorded_endpoint(
    tmp_path: Path, command_runtime: CommandRuntime, redis_access: None
) -> None:
    ports = new_home_ports()
    ports["redis"] = 17380
    (tmp_path / "start-intent.json").write_text(
        json.dumps({"record": {"ports": ports, "gateway_home": str(tmp_path), "created_at": "t"}})
    )
    command_runtime.config.set_field("cluster_secret", "bearer")
    command_runtime.config.set_field("db_url", "postgresql://meiva@127.0.0.1:6433/meiva")
    command_runtime.config.set_field("redis_url", "redis://meiva:secret@127.0.0.1:17380/0")
    command_runtime.config.set_field("machine_host", "10.64.0.7")
    command_runtime.machine.reset()

    status = bridge.probe_bridge_on(
        "darwin", tmp_path, machine=command_runtime.machine, config=command_runtime.config
    )

    assert status.required
    assert not status.serving
    assert status.endpoint == "10.64.0.7:17380"


def test_the_door_probes_on_the_hosts_platform_and_prints_one_line(
    tmp_path: Path, command_runtime: CommandRuntime, capsys: pytest.CaptureFixture[str]
) -> None:
    command_runtime.config.set_field("machine_host", "127.0.0.1")
    command_runtime.machine.reset()

    status = bridge.probe_redis_bridge(
        tmp_path,
        host=command_runtime.host,
        machine=command_runtime.machine,
        config=command_runtime.config,
    )
    bridge.print_redis_bridge_status(
        tmp_path,
        host=command_runtime.host,
        machine=command_runtime.machine,
        config=command_runtime.config,
    )

    assert status == not_required()
    assert capsys.readouterr().out == "  · not required (Redis has no local off-box ingress)\n"


@pytest.mark.parametrize(("platform", "needed"), [(DARWIN_ARM64, True), (LINUX_X86, False)])
def test_the_door_answers_for_the_platform_the_host_reports(
    platform: Platform, needed: bool, tmp_path: Path, command_runtime: CommandRuntime
) -> None:
    command_runtime.config.set_field("db_url", "postgresql://meiva@127.0.0.1:6433/meiva")
    command_runtime.config.set_field("redis_url", "redis://meiva:secret@127.0.0.1:6380/0")
    command_runtime.config.set_field("machine_host", "10.64.0.7")
    command_runtime.machine.reset()

    status = bridge.probe_redis_bridge(
        tmp_path,
        host=fake_host(platform=platform),
        machine=command_runtime.machine,
        config=command_runtime.config,
    )

    assert status.required is needed
