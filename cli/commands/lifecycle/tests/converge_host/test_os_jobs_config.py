# meiva:note
# The scheduled-job converge steps reach the real registrars with the owner's live policy.
#
# The platform scheduler is the one resource a test home cannot isolate, so the registrars write
# into the fake OS user's `~/Library/LaunchAgents` (`default_home`) and talk to a scheduler that is
# a stand-in for `launchctl` (macOS) or `crontab` (Linux). Every assertion is on what ended up
# scheduled, after the configuration the steps read changed under them.

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from base.host.proc import CommandRunner
from base.host.system.tests.fake_commands import (
    DARWIN_ARM64,
    LINUX_X86,
    FakeCommands,
    Reply,
    fake_host,
)
from base.host.system.tests.fake_crontab import FakeCrontab
from cli.commands.converge.os_jobs import (
    ensure_cluster_autostart,
    ensure_health_probe_cron,
    ensure_logs_maintenance,
    ensure_packages_refresh_job,
    ensure_pr_flow_job,
    ensure_walg_job,
)
from cli.commands.converge.spec import ConvergeCtx

_LABELS = ("health-probe", "autostart", "logs-maintenance", "packages-refresh", "walg")


# meiva:note
# A context on the fake OS user's default home, with every job policy switched on.
def _scheduler_ctx(
    ctx: ConvergeCtx, default_home: Path, scheduler: CommandRunner, *, mac: bool
) -> ConvergeCtx:
    for name, value in [
        ("os_jobs_enabled", True),
        ("refresh_enabled", True),
        ("refresh_tick_seconds", 1200),
        ("backup_hour", 21),
        ("walg_config_file", default_home / "walg.json"),
    ]:
        ctx.config.set_field(name, value)
    host = fake_host(
        scheduler,
        platform=DARWIN_ARM64 if mac else LINUX_X86,
        programs={"crontab": "/usr/bin/crontab"},
    )
    return replace(
        ctx,
        host=host,
        meiva_home=default_home / ".meiva",
        roles=None,
        services=frozenset({"gateway", "frontend"}),
    )


def _launchctl() -> FakeCommands:
    launchctl = FakeCommands()
    launchctl.on("launchctl", Reply(), repeat=True)
    return launchctl


def _launch_agents(default_home: Path) -> Path:
    return default_home / "Library" / "LaunchAgents"


def _plist(default_home: Path, label: str) -> Path:
    return _launch_agents(default_home) / f"com.meiva.{label}.plist"


def _run_formal_steps(ctx: ConvergeCtx, *, autostart: bool = True) -> None:
    for step in (
        ensure_health_probe_cron,
        ensure_cluster_autostart if autostart else None,
        ensure_logs_maintenance,
        ensure_packages_refresh_job,
        ensure_pr_flow_job,
        ensure_walg_job,
    ):
        if step is not None:
            step(ctx)


def test_the_formal_steps_schedule_every_job_on_macos(ctx: ConvergeCtx, default_home: Path) -> None:
    mac = _scheduler_ctx(ctx, default_home, _launchctl(), mac=True)

    _run_formal_steps(mac)

    for label in _LABELS:
        assert _plist(default_home, label).is_file(), label
    # meiva:note
    # Only a production home whose machine holds the sampler's credentials gets the PR-flow job.
    assert not _plist(default_home, "pr-flow").exists()
    autostart = _plist(default_home, "autostart").read_text()
    # meiva:note
    # the boot job launches exactly the services this start selected
    assert "<string>--only-service</string>" in autostart
    assert autostart.index("<string>frontend</string>") < autostart.index(
        "<string>gateway</string>"
    )


def test_the_formal_steps_follow_the_owners_live_policy_on_macos(
    ctx: ConvergeCtx, default_home: Path
) -> None:
    mac = _scheduler_ctx(ctx, default_home, _launchctl(), mac=True)
    _run_formal_steps(mac)
    initial = {label: _plist(default_home, label).read_text() for label in _LABELS}
    assert "<integer>1200</integer>" in initial["packages-refresh"]
    # meiva:note
    # the hour 21 backup, in the job's own time
    assert "<integer>0</integer>" in initial["walg"]

    mac.config.set_field("refresh_tick_seconds", 1800)
    mac.config.set_field("backup_hour", 23)
    _run_formal_steps(mac)

    changed = {label: _plist(default_home, label).read_text() for label in _LABELS}
    assert "<integer>1800</integer>" in changed["packages-refresh"]
    assert "<integer>2</integer>" in changed["walg"]
    # meiva:note
    # nothing it reads changed
    assert changed["health-probe"] == initial["health-probe"]

    mac.config.set_field("os_jobs_enabled", False)
    mac.config.set_field("refresh_tick_seconds", 3600)
    _run_formal_steps(mac)
    assert _plist(default_home, "packages-refresh").read_text() == changed["packages-refresh"], (
        "with OS jobs switched off the steps register nothing"
    )

    mac.config.set_field("walg_config_file", None)
    ensure_walg_job(mac)
    assert not _plist(default_home, "walg").exists(), "clearing the key retires the existing job"


@pytest.mark.parametrize("refresh_enabled", [True, False])
def test_the_packages_refresh_job_follows_its_channel_switch(
    refresh_enabled: bool, ctx: ConvergeCtx, default_home: Path
) -> None:
    mac = _scheduler_ctx(ctx, default_home, _launchctl(), mac=True)
    mac.config.set_field("refresh_enabled", refresh_enabled)

    ensure_packages_refresh_job(mac)

    assert _plist(default_home, "packages-refresh").exists() is refresh_enabled


def test_the_crontab_jobs_are_scheduled_and_follow_the_owners_live_policy_on_linux(
    ctx: ConvergeCtx, default_home: Path
) -> None:
    crontab = FakeCrontab()
    linux = _scheduler_ctx(ctx, default_home, crontab, mac=False)

    def lines(marker: str) -> list[str]:
        return [line for line in crontab.lines if line.endswith(marker)]

    def run() -> None:
        # meiva:note
        # The boot job is a systemd unit on Linux; it has its own tests beside its registrar.
        _run_formal_steps(linux, autostart=False)

    run()
    assert [len(lines(m)) for m in ("# meiva-health-probe", "# meiva-logs-maintenance")] == [1, 1]
    assert lines("# meiva-packages-refresh")[0].startswith("*/20 ")
    assert lines("# meiva-walg")[0].startswith("25 0 ")

    linux.config.set_field("refresh_tick_seconds", 1800)
    linux.config.set_field("backup_hour", 23)
    run()
    assert lines("# meiva-packages-refresh")[0].startswith("*/30 ")
    assert lines("# meiva-walg")[0].startswith("25 2 ")
    assert len(lines("# meiva-walg")) == 1, "re-registering replaces the job"

    linux.config.set_field("walg_config_file", None)
    ensure_walg_job(linux)
    assert lines("# meiva-walg") == [], "clearing the key retires the existing job"
