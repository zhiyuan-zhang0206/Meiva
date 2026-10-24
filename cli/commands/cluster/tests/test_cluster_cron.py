# meiva:note
# `meiva cluster health-probe-register` and `-unregister`: the OS cron job of the health probe.
#
# The registrar belongs to the default home only (a scratch home must never replace the host's
# real job), so a disabled gate or a scratch home does nothing at all, and a default home on a
# host with no crontab is refused with the reason. The host is an inert one that records every
# command the registrar would run.

from __future__ import annotations

from pathlib import Path

import pytest

from base.host.system.tests.fake_commands import FakeCommands, Reply, fake_host
from base.paths.tests.homes import default_home as default_home
from base.paths.tests.homes import unit_home as unit_home
from cli.commands.cluster.cron import cmd_cron_register, cmd_cron_unregister


def test_a_disabled_os_job_gate_registers_nothing(unit_home: Path) -> None:
    commands = FakeCommands()

    assert cmd_cron_register(os_jobs_enabled=False, interval_s=420, host=fake_host(commands)) == 0

    assert commands.calls == []


def test_a_scratch_home_never_reaches_the_hosts_scheduler(unit_home: Path) -> None:
    commands = FakeCommands()

    assert cmd_cron_register(os_jobs_enabled=True, interval_s=420, host=fake_host(commands)) == 0
    assert cmd_cron_unregister(host=fake_host(commands)) == 0

    assert commands.calls == []


def test_a_default_home_on_a_host_without_a_crontab_skips_with_a_note(
    default_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    commands = FakeCommands()

    rc = cmd_cron_register(os_jobs_enabled=True, interval_s=420, host=fake_host(commands))

    assert rc == 0
    assert "crontab not installed on this host (skipping)" in capsys.readouterr().out
    assert commands.calls == []


def test_a_crontab_the_host_refuses_to_write_is_a_failed_registration_with_the_reason(
    default_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    commands = FakeCommands()
    commands.on(("crontab", "-l"), Reply())
    commands.on(("crontab", "-"), Reply(returncode=1, stderr="crontab: denied"))
    host = fake_host(commands, programs={"crontab": "/usr/bin/crontab"})

    rc = cmd_cron_register(os_jobs_enabled=True, interval_s=420, host=host)

    assert rc == 1
    assert capsys.readouterr().out.endswith("  * cron registration failed on Linux\n")
    assert ("crontab", "-") in commands.argvs
