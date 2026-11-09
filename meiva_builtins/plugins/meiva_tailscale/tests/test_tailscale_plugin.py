# meiva:note
# Unit tests for the meiva_tailscale builtin keepalive plugin.

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
from meiva_builtins.plugins.meiva_tailscale import default_config, plugin
from meiva_builtins.plugins.meiva_tailscale.default_config import TailscaleConfig
from meiva_builtins.plugins.meiva_tailscale.service_declaration.daemon import (
    CommandResult,
    WatchdogState,
    close_watchdog_socket_server,
    parse_tailscale_backend_state,
    reconcile_tailscale_once,
    resolve_tailscale_binary,
    start_watchdog_socket_server,
)
from meiva_builtins.plugins.meiva_tailscale.service_declaration.declaration import (
    probe_tailscale,
    watchdog_pidfile_path,
    watchdog_socket_path,
)
from meiva_builtins.plugins.meiva_tailscale.services import services
from services.supervision.roster.composition import build_services


def _endpoints() -> ServiceEndpoints:
    return ServiceEndpoints({})


def test_default_config_validation() -> None:
    cfg = TailscaleConfig()
    assert cfg.tailscale_enabled is True
    assert cfg.tailscale_bin == "tailscale"
    assert cfg.tailscale_auto_up is True
    assert cfg.tailscale_up_args == []
    assert cfg.tailscale_check_interval_seconds == 30.0

    custom = TailscaleConfig(
        tailscale_enabled=False,
        tailscale_bin="/opt/homebrew/bin/tailscale",
        tailscale_auto_up=False,
        tailscale_up_args=["--accept-routes"],
        tailscale_check_interval_seconds=60.0,
    )
    assert custom.tailscale_enabled is False
    assert custom.tailscale_up_args == ["--accept-routes"]

    with pytest.raises(ValidationError):
        _ = TailscaleConfig(tailscale_bin="")

    with pytest.raises(ValidationError):
        _ = TailscaleConfig(tailscale_check_interval_seconds=2.0)

    with pytest.raises(ValidationError):
        _ = TailscaleConfig(tailscale_check_interval_seconds=900.0)

    assert default_config.contribute().config is TailscaleConfig
    assert plugin.contribute().config is None


def test_services_declaration_and_gate_toggle(tmp_path: Path) -> None:
    specs = services(home=tmp_path, endpoints=_endpoints())
    assert len(specs) == 1
    spec = specs[0]
    cfg_path = disk_image_path("meiva_tailscale", home=tmp_path)
    assert spec.session == "tailscale"
    assert (
        spec.cmd
        == ".venv/bin/python -m meiva_builtins.plugins.meiva_tailscale.service_declaration.daemon"
    )
    assert spec.pidfile == run_dir(tmp_path) / "tailscale.pid"
    assert spec.pidfile == watchdog_pidfile_path(tmp_path)
    assert spec.capabilities == frozenset({"gateway", "agent-runner"})
    assert spec.requires_db is False
    assert spec.gate is not None
    assert spec.gate() is None
    assert spec.plugin_config == ("meiva_tailscale", TailscaleConfig())
    assert spec.config_inputs == (cfg_path,)

    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    _ = cfg_path.write_text(
        TailscaleConfig(tailscale_enabled=False).model_dump_json(),
        encoding="utf-8",
    )
    disabled_spec = services(home=tmp_path, endpoints=_endpoints())[0]
    assert disabled_spec.gate is not None
    assert disabled_spec.gate() == "disabled (meiva_tailscale.tailscale_enabled off)"


def test_build_services_discovers_tailscale(tmp_path: Path) -> None:
    roster = build_services(home=tmp_path)
    by_session = {spec.session: spec for spec in roster}
    assert "tailscale" in by_session
    assert by_session["tailscale"].plugin_config is not None
    assert by_session["tailscale"].plugin_config[0] == "meiva_tailscale"


def test_binary_resolution_and_backend_state_parsing(tmp_path: Path) -> None:
    fake_bin = tmp_path / "tailscale"
    _ = fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")

    assert resolve_tailscale_binary(
        "tailscale",
        which=lambda _name: None,
        fallback_paths=(fake_bin,),
    ) == str(fake_bin)
    assert resolve_tailscale_binary(
        str(fake_bin),
        which=lambda _name: None,
        fallback_paths=(),
    ) == str(fake_bin)
    assert (
        resolve_tailscale_binary(
            "tailscale",
            which=lambda _name: None,
            fallback_paths=(tmp_path / "missing",),
        )
        is None
    )

    assert parse_tailscale_backend_state('{"BackendState": "Running"}') == "Running"
    assert parse_tailscale_backend_state('{"BackendState": "Stopped"}') == "Stopped"
    assert parse_tailscale_backend_state("not-json") is None
    assert parse_tailscale_backend_state("[]") is None
    assert parse_tailscale_backend_state("") is None


@pytest.mark.asyncio
async def test_reconcile_tailscale_running_stopped_and_macos_recovery(tmp_path: Path) -> None:
    recorded_cmds: list[list[str]] = []
    responses: list[CommandResult] = []

    async def fake_runner(
        cmd: Sequence[str],
        *,
        timeout_seconds: float,
    ) -> CommandResult:
        _ = timeout_seconds
        recorded_cmds.append(list(cmd))
        if responses:
            return responses.pop(0)
        return CommandResult(returncode=0, stdout="", stderr="")

    state = WatchdogState()
    config = TailscaleConfig(tailscale_up_args=["--accept-routes"])
    fake_app = tmp_path / "Tailscale.app"
    fake_app.mkdir()

    responses.append(
        CommandResult(
            returncode=0,
            stdout='{"BackendState": "Running"}',
            stderr="",
        )
    )
    await reconcile_tailscale_once(
        state,
        config,
        which=lambda _name: "/usr/local/bin/tailscale",
        is_darwin=True,
        runner=fake_runner,
        macos_app_path=fake_app,
    )
    assert recorded_cmds == [["/usr/local/bin/tailscale", "status", "--json"]]
    assert state.last_backend_state == "Running"

    recorded_cmds.clear()
    responses.extend(
        [
            CommandResult(
                returncode=0,
                stdout='{"BackendState": "Stopped"}',
                stderr="",
            ),
            CommandResult(returncode=0, stdout="", stderr=""),
        ]
    )
    await reconcile_tailscale_once(
        state,
        config,
        which=lambda _name: "/usr/local/bin/tailscale",
        is_darwin=True,
        runner=fake_runner,
        macos_app_path=fake_app,
    )
    assert recorded_cmds == [
        ["/usr/local/bin/tailscale", "status", "--json"],
        ["/usr/local/bin/tailscale", "up", "--accept-routes"],
    ]

    recorded_cmds.clear()
    responses.extend(
        [
            CommandResult(
                returncode=1,
                stdout="",
                stderr="failed to connect to local Tailscale daemon",
            ),
            CommandResult(returncode=0, stdout="", stderr=""),
            CommandResult(returncode=0, stdout="", stderr=""),
        ]
    )
    await reconcile_tailscale_once(
        state,
        config,
        which=lambda _name: "/usr/local/bin/tailscale",
        is_darwin=True,
        runner=fake_runner,
        macos_app_path=fake_app,
    )
    assert recorded_cmds == [
        ["/usr/local/bin/tailscale", "status", "--json"],
        ["open", "-g", "-a", "Tailscale"],
        ["/usr/local/bin/tailscale", "up", "--accept-routes"],
    ]

    for cmd in recorded_cmds:
        assert "down" not in cmd


@pytest.mark.asyncio
async def test_watchdog_unix_socket_probe() -> None:
    with tempfile.TemporaryDirectory(prefix="meiva-ts-", dir="/tmp") as raw_dir:
        short_home = Path(raw_dir)
        sock_path = watchdog_socket_path(short_home)

        down_before = probe_tailscale(short_home)
        assert down_before.verdict == ProbeVerdict.DOWN
        assert down_before.alive is False

        server = await start_watchdog_socket_server(sock_path)
        try:
            up_probe = await asyncio.to_thread(probe_tailscale, short_home)
            assert up_probe.verdict == ProbeVerdict.ALIVE
            assert up_probe.alive is True
        finally:
            await close_watchdog_socket_server(server, sock_path)

        down_after = probe_tailscale(short_home)
        assert down_after.verdict == ProbeVerdict.DOWN
        assert down_after.alive is False
