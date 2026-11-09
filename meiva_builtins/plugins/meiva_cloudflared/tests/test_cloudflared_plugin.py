# meiva:note
# Unit tests for the meiva_cloudflared builtin keepalive plugin.

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from base.daemon.endpoints import ServiceEndpoints
from base.daemon.health import ProbeVerdict
from base.packages.plugins.config_registration import disk_image_path
from base.paths import run_dir
from meiva_builtins.plugins.meiva_cloudflared import default_config, plugin
from meiva_builtins.plugins.meiva_cloudflared.default_config import CloudflaredConfig
from meiva_builtins.plugins.meiva_cloudflared.service_declaration.daemon import (
    ManagedProcess,
    ProcessSnapshot,
    WatchdogState,
    build_cloudflared_command,
    close_watchdog_socket_server,
    find_external_cloudflared_pid,
    is_cloudflared_tunnel_process,
    reconcile_cloudflared_once,
    resolve_cloudflared_binary,
    resolve_cloudflared_config_path,
    start_watchdog_socket_server,
    terminate_managed_child,
)
from meiva_builtins.plugins.meiva_cloudflared.service_declaration.declaration import (
    probe_cloudflared,
    watchdog_pidfile_path,
    watchdog_socket_path,
)
from meiva_builtins.plugins.meiva_cloudflared.services import services
from services.supervision.roster.composition import build_services


class _FakeManagedProcess:
    def __init__(
        self,
        pid: int = 4242,
        returncode: int | None = None,
        *,
        hang_on_terminate: bool = False,
    ) -> None:
        self._pid = pid
        self._returncode = returncode
        self._hang_on_terminate = hang_on_terminate
        self.terminated = False
        self.killed = False

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def returncode(self) -> int | None:
        return self._returncode

    def set_returncode(self, code: int | None) -> None:
        self._returncode = code

    def terminate(self) -> None:
        self.terminated = True
        if not self._hang_on_terminate:
            self._returncode = 0

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9

    async def wait(self) -> int:
        if self._hang_on_terminate and not self.killed:
            await asyncio.sleep(10.0)
        return 0 if self._returncode is None else self._returncode


def _endpoints() -> ServiceEndpoints:
    return ServiceEndpoints({})


def test_default_config_validation() -> None:
    cfg = CloudflaredConfig()
    assert cfg.cloudflared_enabled is True
    assert cfg.cloudflared_bin == "cloudflared"
    assert cfg.cloudflared_config_path == ""
    assert cfg.cloudflared_tunnel == ""
    assert cfg.cloudflared_extra_args == []
    assert cfg.cloudflared_check_interval_seconds == 15.0

    custom = CloudflaredConfig(
        cloudflared_enabled=False,
        cloudflared_bin="/opt/homebrew/bin/cloudflared",
        cloudflared_config_path="~/.cloudflared/config.yml",
        cloudflared_tunnel="my-tunnel",
        cloudflared_extra_args=["--loglevel", "info"],
        cloudflared_check_interval_seconds=30.0,
    )
    assert custom.cloudflared_enabled is False
    assert custom.cloudflared_tunnel == "my-tunnel"

    with pytest.raises(ValidationError):
        _ = CloudflaredConfig(cloudflared_bin="")

    with pytest.raises(ValidationError):
        _ = CloudflaredConfig(cloudflared_check_interval_seconds=1.0)

    with pytest.raises(ValidationError):
        _ = CloudflaredConfig(cloudflared_check_interval_seconds=500.0)

    assert default_config.contribute().config is CloudflaredConfig
    assert plugin.contribute().config is None


def test_services_declaration_and_gate_toggle(tmp_path: Path) -> None:
    specs = services(home=tmp_path, endpoints=_endpoints())
    assert len(specs) == 1
    spec = specs[0]
    cfg_path = disk_image_path("meiva_cloudflared", home=tmp_path)
    assert spec.session == "cloudflared"
    assert (
        spec.cmd
        == ".venv/bin/python -m meiva_builtins.plugins.meiva_cloudflared.service_declaration.daemon"
    )
    assert spec.pidfile == run_dir(tmp_path) / "cloudflared.pid"
    assert spec.pidfile == watchdog_pidfile_path(tmp_path)
    assert spec.capabilities == frozenset({"gateway", "agent-runner"})
    assert spec.requires_db is False
    assert spec.gate is not None
    assert spec.gate() is None
    assert spec.plugin_config == ("meiva_cloudflared", CloudflaredConfig())
    assert spec.config_inputs == (cfg_path,)

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    _ = cfg_path.write_text(
        CloudflaredConfig(cloudflared_enabled=False).model_dump_json(),
        encoding="utf-8",
    )
    disabled_spec = services(home=tmp_path, endpoints=_endpoints())[0]
    assert disabled_spec.gate is not None
    assert disabled_spec.gate() == "disabled (meiva_cloudflared.cloudflared_enabled off)"


def test_build_services_discovers_cloudflared(tmp_path: Path) -> None:
    roster = build_services(home=tmp_path)
    by_session = {spec.session: spec for spec in roster}
    assert "cloudflared" in by_session
    assert by_session["cloudflared"].plugin_config is not None
    assert by_session["cloudflared"].plugin_config[0] == "meiva_cloudflared"


def test_process_detection_and_command_builder(tmp_path: Path) -> None:
    self_pid = 1000
    watchdog_proc = ProcessSnapshot(
        pid=self_pid,
        name="python3",
        cmdline=(
            "/usr/bin/python3",
            "-m",
            "meiva_builtins.plugins.meiva_cloudflared.service_declaration.daemon",
        ),
    )
    other_python = ProcessSnapshot(
        pid=1001,
        name="python3",
        cmdline=("/usr/bin/python3", "-m", "some_other_module", "tunnel"),
    )
    version_proc = ProcessSnapshot(
        pid=1002,
        name="cloudflared",
        cmdline=("/opt/homebrew/bin/cloudflared", "--version"),
    )
    external_tunnel = ProcessSnapshot(
        pid=2048,
        name="cloudflared",
        cmdline=(
            "/opt/homebrew/bin/cloudflared",
            "--no-autoupdate",
            "tunnel",
            "run",
        ),
    )

    assert not is_cloudflared_tunnel_process(watchdog_proc, self_pid=self_pid)
    assert not is_cloudflared_tunnel_process(other_python, self_pid=self_pid)
    assert not is_cloudflared_tunnel_process(version_proc, self_pid=self_pid)
    assert is_cloudflared_tunnel_process(external_tunnel, self_pid=self_pid)

    assert (
        find_external_cloudflared_pid(
            [watchdog_proc, other_python, version_proc, external_tunnel],
            self_pid=self_pid,
        )
        == 2048
    )

    cfg_dir = tmp_path / ".cloudflared"
    cfg_dir.mkdir(parents=True)
    cfg_file = cfg_dir / "config.yml"
    _ = cfg_file.write_text("tunnel: test-id\n", encoding="utf-8")

    resolved = resolve_cloudflared_config_path(
        "",
        user_home=tmp_path,
        system_config_dir=tmp_path / "etc",
    )
    assert resolved == cfg_file

    explicit = resolve_cloudflared_config_path(
        "~/.cloudflared/config.yml",
        user_home=tmp_path,
        system_config_dir=tmp_path / "etc",
    )
    assert explicit == cfg_file

    fake_bin = tmp_path / "cloudflared"
    _ = fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    assert resolve_cloudflared_binary(
        "cloudflared",
        which=lambda _name: None,
        fallback_paths=(fake_bin,),
    ) == str(fake_bin)

    cmd = build_cloudflared_command(
        str(fake_bin),
        CloudflaredConfig(
            cloudflared_tunnel="my-tunnel",
            cloudflared_extra_args=["--loglevel", "warn"],
        ),
        resolved_config_path=cfg_file,
    )
    assert cmd == [
        str(fake_bin),
        "--no-autoupdate",
        "--config",
        str(cfg_file),
        "tunnel",
        "--loglevel",
        "warn",
        "run",
        "my-tunnel",
    ]


@pytest.mark.asyncio
async def test_reconcile_external_vs_managed_and_safe_shutdown(tmp_path: Path) -> None:
    cfg_dir = tmp_path / ".cloudflared"
    cfg_dir.mkdir(parents=True)
    cfg_file = cfg_dir / "config.yml"
    _ = cfg_file.write_text("tunnel: demo\n", encoding="utf-8")

    spawned_cmds: list[list[str]] = []
    fake_children: list[_FakeManagedProcess] = []

    async def fake_spawner(cmd: Sequence[str]) -> ManagedProcess:
        spawned_cmds.append(list(cmd))
        child = _FakeManagedProcess(pid=5000 + len(fake_children))
        fake_children.append(child)
        return child

    state = WatchdogState()
    config = CloudflaredConfig()

    external_proc = ProcessSnapshot(
        pid=999,
        name="cloudflared",
        cmdline=("/opt/homebrew/bin/cloudflared", "tunnel", "run"),
    )
    await reconcile_cloudflared_once(
        state,
        config,
        self_pid=100,
        user_home=tmp_path,
        which=lambda _name: "/opt/homebrew/bin/cloudflared",
        process_lister=lambda: [external_proc],
        spawner=fake_spawner,
    )
    assert spawned_cmds == []
    assert state.managed_proc is None
    assert state.last_external_pid == 999

    await terminate_managed_child(state.managed_proc)

    await reconcile_cloudflared_once(
        state,
        config,
        self_pid=100,
        user_home=tmp_path,
        which=lambda _name: "/opt/homebrew/bin/cloudflared",
        process_lister=tuple,
        spawner=fake_spawner,
    )
    assert len(spawned_cmds) == 1
    assert state.managed_proc is fake_children[0]

    await reconcile_cloudflared_once(
        state,
        config,
        self_pid=100,
        user_home=tmp_path,
        which=lambda _name: "/opt/homebrew/bin/cloudflared",
        process_lister=tuple,
        spawner=fake_spawner,
    )
    assert len(spawned_cmds) == 1

    fake_children[0].set_returncode(1)
    await reconcile_cloudflared_once(
        state,
        config,
        self_pid=100,
        user_home=tmp_path,
        which=lambda _name: "/opt/homebrew/bin/cloudflared",
        process_lister=tuple,
        spawner=fake_spawner,
    )
    assert len(spawned_cmds) == 2
    assert state.managed_proc is fake_children[1]

    await terminate_managed_child(state.managed_proc)
    assert fake_children[1].terminated is True
    assert fake_children[1].killed is False

    hung_child = _FakeManagedProcess(pid=7777, hang_on_terminate=True)
    await terminate_managed_child(hung_child, grace_seconds=0.05)
    assert hung_child.terminated is True
    assert hung_child.killed is True


@pytest.mark.asyncio
async def test_watchdog_unix_socket_probe() -> None:
    with tempfile.TemporaryDirectory(prefix="meiva-cf-", dir="/tmp") as raw_dir:
        short_home = Path(raw_dir)
        sock_path = watchdog_socket_path(short_home)

        down_before = probe_cloudflared(short_home)
        assert down_before.verdict == ProbeVerdict.DOWN
        assert down_before.alive is False

        server = await start_watchdog_socket_server(sock_path)
        try:
            up_probe = await asyncio.to_thread(probe_cloudflared, short_home)
            assert up_probe.verdict == ProbeVerdict.ALIVE
            assert up_probe.alive is True
        finally:
            await close_watchdog_socket_server(server, sock_path)

        down_after = probe_cloudflared(short_home)
        assert down_after.verdict == ProbeVerdict.DOWN
        assert down_after.alive is False
