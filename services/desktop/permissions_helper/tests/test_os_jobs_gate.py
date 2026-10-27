# meiva:note
# The OS-job gates, and what a generated job spec is anchored to.
#
# `MEIVA_OS_JOBS_ENABLED` stops the suite from arming a job at all; the default-home
# gate (`owns_os_job_home`) stops any other home from registering or removing one,
# because labels and crontab markers name a job, not a home; and the anchoring makes
# any job that IS armed name the binary and `$MEIVA_HOME` of the checkout that wrote
# it — so no stale job can resolve onto the prod install.

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path

import pytest

from base.config import settings
from base.host.system import logs_content, logs_job, packages_job, pr_flow_job, walg_job
from base.host.system.boot_autostart import registration as autostart
from base.host.system.boot_autostart.content import launchd_plist_content
from base.host.system.cron import health_content, health_job, mechanics
from base.host.system.hostport import Host
from base.host.system.job_values import JobRoots, cron_environment, plist_path
from base.host.system.packages_content import RefreshSchedule
from base.host.system.tests.fake_commands import DARWIN_ARM64, FakeCommands, Reply, fake_host
from base.sessions.tests.refuse_helper import refuse_helper
from services.desktop.permissions_helper.tests.support import (
    helper_build_env,
    helper_install_env,
    refuse_tools,
)


@dataclass(frozen=True)
class _ProbePlatform:
    system: str = "probe"
    machine: str = "x86_64"
    python: str = "/usr/bin/python3"


_PROBE_PLATFORM = _ProbePlatform()


# meiva:note
# Build a Host whose platform system ("probe") is neither darwin nor linux so
# reaching the platform dispatcher raises RuntimeError before touching disk or OS jobs.
def _probe_host(user_home: Path | None = None) -> Host:
    if user_home is not None:
        token = user_home / ".trunk" / "api-token"
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text("test-token\n", encoding="utf-8")
    return fake_host(
        platform=_PROBE_PLATFORM,  # pyright: ignore[reportArgumentType]
        programs={"gh": "/usr/bin/gh"},
    )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_suite_default_is_off() -> None:
    """The whole suite runs with registration disabled — this is the invariant the
    e2e leak violated (nine launchd health probes on a dev box), so assert it
    directly rather than trusting the conftest comment."""
    assert settings.general.os_jobs_enabled is False


def _register_calls(enabled: bool) -> list[object]:
    return [
        pytest.param(
            partial(health_job.register_os_cron, enabled=enabled),
            id="health-probe",
        ),
        pytest.param(
            partial(
                autostart.register_autostart,
                services=frozenset({"gateway"}),
                enabled=enabled,
            ),
            id="autostart",
        ),
        pytest.param(
            partial(logs_job.register_logs_job, enabled=enabled),
            id="logs-maintenance",
        ),
        pytest.param(
            partial(
                packages_job.register_packages_job,
                enabled=enabled,
                schedule=RefreshSchedule(
                    refresh_enabled=settings.packages.refresh_enabled,
                    tick_seconds=settings.packages.refresh_tick_seconds,
                ),
            ),
            id="packages-refresh",
        ),
        pytest.param(
            partial(
                pr_flow_job.register_pr_flow_job,
                enabled=enabled,
            ),
            id="pr-flow",
        ),
        pytest.param(
            partial(
                walg_job.register_walg_job,
                enabled=enabled,
                backup_hour=settings.services.backup_hour,
            ),
            id="walg",
        ),
    ]


_REGISTERS = _register_calls(settings.general.os_jobs_enabled)
_REGISTERS_ENABLED = _register_calls(True)
_UNREGISTER_CALLS: dict[str, Callable[..., None]] = {
    "health-probe": health_job.unregister_os_cron,
    "autostart": autostart.unregister_autostart,
    "logs-maintenance": logs_job.unregister_logs_job,
    "packages-refresh": packages_job.unregister_packages_job,
    "pr-flow": pr_flow_job.unregister_pr_flow_job,
    "walg": walg_job.unregister_walg_job,
}
_UNREGISTERS = [pytest.param(call, id=name) for name, call in _UNREGISTER_CALLS.items()]


@pytest.mark.parametrize("call", _REGISTERS)
def test_registration_never_reaches_the_backend_when_gated(
    call: object,
    default_home: Path,
) -> None:
    host = _probe_host(default_home)
    roots = JobRoots(home=default_home / ".meiva", repo=default_home, user_home=default_home)
    assert callable(call)
    if isinstance(call, partial) and call.func is pr_flow_job.register_pr_flow_job:
        call(production=True, roots=roots, host=host)
    else:
        call(roots=roots, host=host)


def test_registration_dispatches_when_enabled(
    default_home: Path,
) -> None:
    host = _probe_host(default_home)
    roots = JobRoots(home=default_home / ".meiva", repo=default_home, user_home=default_home)
    calls: list[Callable[[], None]] = [
        lambda: health_job.register_os_cron(enabled=True, host=host, roots=roots),
        lambda: autostart.register_autostart(
            services=frozenset({"gateway"}),
            enabled=True,
            host=host,
            roots=roots,
        ),
        lambda: logs_job.register_logs_job(roots=roots, enabled=True, host=host),
        lambda: packages_job.register_packages_job(
            roots=roots,
            enabled=True,
            schedule=RefreshSchedule(
                refresh_enabled=settings.packages.refresh_enabled,
                tick_seconds=settings.packages.refresh_tick_seconds,
            ),
            host=host,
        ),
        lambda: pr_flow_job.register_pr_flow_job(
            roots=roots,
            enabled=True,
            production=True,
            host=host,
        ),
        lambda: walg_job.register_walg_job(
            roots=roots,
            enabled=True,
            backup_hour=settings.services.backup_hour,
            host=host,
        ),
    ]
    for call in calls:
        with pytest.raises(RuntimeError, match="unsupported host platform for OS jobs"):
            call()


def test_deregistration_is_not_gated_by_the_os_jobs_switch(
    default_home: Path,
) -> None:
    """Cleanup has to work wherever registration is switched off (the suite runs
    with it off) — otherwise a leak under an older build can never be swept."""
    host = _probe_host()
    roots = JobRoots(home=default_home / ".meiva", repo=default_home, user_home=default_home)
    for call in _UNREGISTER_CALLS.values():
        with pytest.raises(RuntimeError, match="unsupported host platform for OS jobs"):
            call(host=host, roots=roots)


# ---------------------------------------------------------------------------
# The default-home gate
# ---------------------------------------------------------------------------
# Labels and crontab markers name a job, not a home, so the scheduler's one
# namespace per OS user is shared by every process on the host. A scratch home
# (a test, a tool, a disposable cluster) must neither replace nor remove the
# host's real job — on a development machine that also runs production, a
# `launchctl bootout` from a test would take a production job down.


@pytest.mark.parametrize("call", _REGISTERS_ENABLED)
def test_a_scratch_home_registers_nothing(
    call: object,
    unit_home: Path,
) -> None:
    host = _probe_host(unit_home)
    roots = JobRoots(home=unit_home, repo=unit_home, user_home=unit_home)
    assert callable(call)
    if isinstance(call, partial) and call.func is pr_flow_job.register_pr_flow_job:
        call(production=True, roots=roots, host=host)
    else:
        call(roots=roots, host=host)


@pytest.mark.parametrize("call", _UNREGISTERS)
def test_a_scratch_home_removes_nothing(call: object, unit_home: Path) -> None:
    assert callable(call)
    call(host=_probe_host(), roots=JobRoots(home=unit_home, repo=unit_home, user_home=unit_home))


def test_the_default_home_owns_the_jobs_however_it_is_spelled(
    default_home: Path,
) -> None:
    roots = JobRoots(home=default_home / ".meiva", repo=default_home, user_home=default_home)
    assert mechanics.owns_os_job_home("autostart", roots=roots)
    assert mechanics.owns_os_job_home(
        "autostart", roots=replace(roots, home=default_home / "elsewhere" / ".." / ".meiva")
    )
    assert not mechanics.owns_os_job_home(
        "autostart", roots=replace(roots, home=default_home / ".meiva-preview")
    )


# ---------------------------------------------------------------------------
# What a generated job spec is anchored to
# ---------------------------------------------------------------------------


def test_the_meiva_binary_falls_back_to_path_without_a_venv(
    tmp_path: Path,
) -> None:
    commands = FakeCommands()
    commands.on("launchctl", Reply("", "", 0), repeat=True)
    host = fake_host(
        commands,
        platform=DARWIN_ARM64,
        programs={"meiva": "/usr/local/bin/meiva"},
    )
    roots = JobRoots(home=tmp_path / "home", repo=tmp_path / "repo", user_home=tmp_path / "user")
    assert logs_job.register_macos(roots=roots, host=host) == 0
    written = plist_path(logs_content.LABEL, user_home=roots.user_home).read_text(encoding="utf-8")
    assert "/usr/local/bin/meiva" in written


def test_health_probe_plist_pins_meiva_home(tmp_path: Path) -> None:
    body = health_content.launchd_plist_content(
        arguments=("/x/meiva", "cluster", "health-probe"),
        interval_seconds=300,
        log_file=tmp_path / "independent-log-home/out.log",
        environment_home=tmp_path / ".meiva-x",
        path_env="/x:/usr/bin",
    )
    import plistlib

    assert plistlib.loads(body.encode())["ProgramArguments"] == [
        "/x/meiva",
        "cluster",
        "health-probe",
    ]
    assert "<key>MEIVA_HOME</key>" in body
    assert f"<string>{tmp_path / '.meiva-x'}</string>" in body
    assert "<key>PATH</key>" in body


def test_every_launchagent_pins_meiva_home(tmp_path: Path) -> None:
    home = tmp_path / ".meiva-x"
    body = launchd_plist_content(
        arguments=("/x/meiva", "start", "--boot", "--only-service", "gateway"),
        retry_interval_seconds=60,
        log_file=home / "logs/autostart.log",
        environment_home=home,
        path_env="/x:/usr/bin",
    )
    assert f"<key>MEIVA_HOME</key>\n        <string>{home}</string>" in body


# meiva:note
# `MEIVA_HOME=<home> <cmd>` and not a bare `MEIVA_HOME=` line: cron applies a
# standalone assignment to the WHOLE crontab, which would silently retarget a
# co-located cluster's entries.
def test_cron_environment_scopes_to_one_command(
    tmp_path: Path,
) -> None:
    prefix = cron_environment(tmp_path / ".meiva-x")
    assert prefix == f"MEIVA_HOME={tmp_path / '.meiva-x'} "
    assert not prefix.startswith("\n")


# meiva:note
# The door is the one native boundary the harness guards: the build, signing and launchd modules
# run a tool only through the runner they are handed, so a test that gives them none cannot reach
# the host, and `converge` itself is refused unless the test opts in.
def test_helper_native_effects_require_explicit_test_boundary(tmp_path: Path) -> None:
    from services.desktop import permissions_helper

    env = helper_install_env(helper_build_env(tmp_path / "home", refuse_tools))
    with pytest.raises(pytest.fail.Exception, match="native effect forbidden"):
        permissions_helper.converge(helper=refuse_helper, env=env)


@pytest.mark.native_permissions_helper
def test_native_helper_opt_in_exposes_real_boundaries_without_running_them() -> None:
    from services.desktop import permissions_helper

    assert permissions_helper.converge.__module__ == "services.desktop.permissions_helper"
