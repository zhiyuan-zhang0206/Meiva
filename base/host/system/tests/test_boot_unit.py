from __future__ import annotations

import dataclasses
import grp
import os
import pwd
import re
from pathlib import Path

import psutil
import pytest

from base.host.env.harness_binaries import require_harness_paths
from base.host.system import boot_unit
from base.host.system.boot_policy import BOOT_MAX_ATTEMPTS, BOOT_RETRY_INTERVAL_S
from base.host.system.boot_unit import (
    START_LIMIT_INTERVAL_S,
    START_TIMEOUT_S,
    BootUnitContext,
    install,
    render_unit,
    systemd_running,
    uninstall,
    unit_context,
    unit_enabled,
    verify_post_publication,
)
from base.host.system.hostport import Host
from base.host.system.job_values import JobRoots
from base.host.system.tests.fake_commands import (
    DARWIN_ARM64,
    LINUX_X86,
    FakeCommands,
    Reply,
    fake_host,
)
from base.native_process.ownership import OwnedProcess

_SYSTEMCTL = {"systemctl": "/usr/bin/systemctl"}


def _linux(
    commands: FakeCommands | None = None,
    *,
    uid: int = 0,
    programs: dict[str, str] | None = None,
) -> Host:
    return fake_host(
        commands or FakeCommands(),
        platform=LINUX_X86,
        uid=uid,
        programs=_SYSTEMCTL if programs is None else programs,
    )


def _runtime_dir(tmp_path: Path) -> Path:
    d = tmp_path / "run-systemd"
    d.mkdir(exist_ok=True)
    return d


def _context(tmp_path: Path) -> BootUnitContext:
    return BootUnitContext(
        home=tmp_path / "home",
        repo=tmp_path / "repo",
        user="meiva-user",
        group="meiva-group",
        home_dir=tmp_path / "user-home",
        services=frozenset({"gateway", "frontend"}),
    )


@pytest.fixture()
def ctx(tmp_path: Path) -> BootUnitContext:
    return _context(tmp_path)


# meiva:note
# The unit is anchored to the cluster the roots name and run by the account that owns the host
# process; nothing is read from the environment of the test.
def test_unit_context_names_the_roots_and_the_account_running_the_host(tmp_path: Path) -> None:
    roots = JobRoots(home=tmp_path / "home", repo=tmp_path / "repo", user_home=tmp_path / "user")
    host = _linux(uid=os.getuid())
    account = pwd.getpwuid(os.getuid())

    context = unit_context(roots=roots, services=frozenset({"gateway"}), host=host)

    assert context == BootUnitContext(
        home=roots.home,
        repo=roots.repo,
        user=account.pw_name,
        group=grp.getgrgid(account.pw_gid).gr_name,
        home_dir=Path(account.pw_dir),
        services=frozenset({"gateway"}),
    )


def test_render_unit_states_the_boot_policy(ctx: BootUnitContext) -> None:
    unit = render_unit(ctx)
    assert "Restart=on-failure" in unit
    assert f"RestartSec={BOOT_RETRY_INTERVAL_S}" in unit
    assert f"StartLimitBurst={BOOT_MAX_ATTEMPTS}" in unit
    assert f"StartLimitIntervalSec={START_LIMIT_INTERVAL_S}" in unit
    assert "StartLimitIntervalSec=0" not in unit
    assert '"start" "--boot"' in unit
    assert f"TimeoutStartSec={START_TIMEOUT_S}" in unit
    assert "RuntimeMaxSec" not in unit
    assert "Type=forking" in unit and "GuessMainPID=no" in unit
    assert "KillMode=process" in unit and "SendSIGKILL=no" in unit
    assert f"PIDFile={boot_unit.root_pid_path(ctx.home)}" in unit


def test_render_unit_binds_home_and_checkout(ctx: BootUnitContext) -> None:
    unit = render_unit(ctx)
    assert "After=network-online.target\n" in unit
    assert "WantedBy=multi-user.target" in unit
    assert f"User={ctx.user}" in unit and f"Group={ctx.group}" in unit
    assert f"WorkingDirectory={ctx.repo}" in unit
    assert f'WorkingDirectory="{ctx.repo}"' not in unit
    assert f'Environment="MEIVA_HOME={ctx.home}"' in unit
    assert "MEIVA_HOST_STATE_DIR" not in unit
    assert f'Environment="HOME={ctx.home_dir}"' in unit
    assert 'Environment="PATH=' in unit
    assert f'ExecStart=:"{ctx.repo}/.venv/bin/python" "-m" "cli.main" "start"' in unit


# meiva:note
# The unit's PATH is the one every service under it inherits, so it must reach the
# directories harness CLIs install into; the check names the unit and the missing
# directories rather than letting a spawn fail later (#5491, #5495).
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def test_the_unit_path_reaches_every_harness_directory(ctx: BootUnitContext) -> None:
    unit = render_unit(ctx)
    match = re.search(r'Environment="PATH=([^"]+)"', unit)

    assert match is not None
    entries = match.group(1).split(os.pathsep)
    assert entries[0] == f"{ctx.repo}/.venv/bin"
    assert require_harness_paths(match.group(1), home=Path(ctx.home_dir), what="the unit")
    assert entries[-4:] == ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]


def test_render_unit_selects_every_service_by_name_sorted(ctx: BootUnitContext) -> None:
    unit = render_unit(ctx)
    assert '"start" "--boot" "--only-service" "frontend" "--only-service" "gateway"' in unit


def test_render_unit_refuses_an_empty_selection(ctx: BootUnitContext) -> None:
    empty = dataclasses.replace(ctx, services=frozenset())
    with pytest.raises(ValueError, match="at least one selected service"):
        render_unit(empty)


def test_render_unit_quotes_without_shell_expansion(ctx: BootUnitContext) -> None:
    odd = dataclasses.replace(ctx, repo=Path('/repo "odd" % $x'))
    unit = render_unit(odd)
    assert 'ExecStart=:"/repo \\"odd\\" %% $x/.venv/bin/python" "-m" "cli.main" "start"' in unit
    assert "MEIVA_BOOT_PROXY_WAIT" not in unit


def test_render_unit_refuses_control_characters(ctx: BootUnitContext) -> None:
    bad = dataclasses.replace(ctx, repo=Path("/repo\nExecStart=/bad"))
    with pytest.raises(ValueError, match="control characters"):
        render_unit(bad)


def test_systemd_running_requires_linux_systemctl_and_the_runtime_dir(
    tmp_path: Path,
) -> None:
    runtime_dir = _runtime_dir(tmp_path)
    assert (
        systemd_running(
            host=fake_host(platform=DARWIN_ARM64, programs=_SYSTEMCTL),
            runtime_dir=runtime_dir,
        )
        is False
    )
    assert systemd_running(host=_linux(programs={}), runtime_dir=runtime_dir) is False
    linux = _linux()
    assert systemd_running(host=linux, runtime_dir=tmp_path / "absent") is False
    assert systemd_running(host=linux, runtime_dir=runtime_dir) is True


def test_unit_enabled_reads_the_one_systemctl_seam(tmp_path: Path) -> None:
    runtime_dir = _runtime_dir(tmp_path)
    commands = FakeCommands()
    commands.on(["systemctl", "is-enabled"], Reply("enabled\n"), Reply("disabled\n"))
    host = _linux(commands)
    assert unit_enabled(host=host, runtime_dir=runtime_dir) is True
    assert commands.argvs == [("systemctl", "is-enabled", "meiva-boot.service")]

    assert unit_enabled(host=host, runtime_dir=runtime_dir) is False
    assert unit_enabled(host=host, runtime_dir=tmp_path / "absent") is False
    assert len(commands.calls) == 2


def _install_seams(tmp_path: Path, *, enabled: bool) -> tuple[FakeCommands, Path, Path]:
    unit_dir = tmp_path / "etc-systemd"
    runtime_dir = _runtime_dir(tmp_path)
    commands = FakeCommands()
    commands.on(["install", "-m", "0644"], Reply(), repeat=True)
    commands.on(["systemctl", "daemon-reload"], Reply(), repeat=True)
    commands.on(
        ["systemctl", "is-enabled"],
        Reply("enabled\n" if enabled else "disabled\n"),
        repeat=True,
    )
    commands.on(["systemctl", "enable", "meiva-boot.service"], Reply(), repeat=True)
    return commands, unit_dir, runtime_dir


def test_install_refuses_without_systemd(ctx: BootUnitContext, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="systemd"):
        install(
            context=ctx,
            host=_linux(programs={}),
            runtime_dir=tmp_path / "absent",
        )


def test_uninstall_removes_only_the_boot_units_paths(ctx: BootUnitContext, tmp_path: Path) -> None:
    units = tmp_path / "etc-systemd"
    units.mkdir()
    absent_runtime = tmp_path / "absent-runtime"
    commands = FakeCommands()
    commands.on(["systemctl", "disable", "--now", "meiva-boot.service"], Reply(), repeat=True)
    commands.on(["rm", "-f"], Reply(), repeat=True)
    commands.on(["systemctl", "daemon-reload"], Reply(), repeat=True)
    host = _linux(commands)

    unit = units / "meiva-boot.service"
    unit.write_text(render_unit(ctx))
    foreign = units / "unrelated.service"
    foreign.write_text("y")
    template = units / "meiva-gateway-egress@.service"
    template.write_text("z")
    steps = uninstall(host=host, unit_dir=units, runtime_dir=absent_runtime)

    recorded = [list(argv) for argv in commands.argvs]
    assert ["systemctl", "disable", "--now", "meiva-boot.service"] in recorded
    assert ["rm", "-f", str(unit)] in recorded
    assert ["systemctl", "daemon-reload"] in recorded
    assert str(foreign) not in str(recorded) and "meiva-gateway-egress" not in str(recorded)
    assert foreign.exists() and template.exists()
    assert steps == [f"removed {unit}"]

    unit.unlink()
    before = list(commands.argvs)
    assert uninstall(host=host, unit_dir=units, runtime_dir=absent_runtime) == []
    assert commands.argvs == before
    unit.write_text(render_unit(ctx))
    assert (
        uninstall(
            host=fake_host(platform=DARWIN_ARM64),
            unit_dir=units,
            runtime_dir=absent_runtime,
        )
        == []
    )
    assert unit.exists()


def _manager_reports(tmp_path: Path, active_state: str) -> tuple[Path, Path, FakeCommands]:
    units = tmp_path / "etc-systemd"
    units.mkdir(exist_ok=True)
    runtime_dir = _runtime_dir(tmp_path)
    commands = FakeCommands()
    commands.on(["systemctl", "disable", "--now", "meiva-boot.service"], Reply(), repeat=True)
    commands.on(["rm", "-f"], Reply(), repeat=True)
    commands.on(["systemctl", "daemon-reload"], Reply(), repeat=True)
    commands.on(
        ["systemctl", "show", "--property=ActiveState", "--value", "meiva-boot.service"],
        Reply(f"{active_state}\n"),
        repeat=True,
    )
    commands.on(["systemctl", "reset-failed", "meiva-boot.service"], Reply(), repeat=True)
    return units, runtime_dir, commands


def test_uninstall_leaves_no_failed_record_behind(ctx: BootUnitContext, tmp_path: Path) -> None:
    units, runtime_dir, commands = _manager_reports(tmp_path, "failed")
    host = _linux(commands)
    name = "meiva-boot.service"
    (units / name).write_text(render_unit(ctx))

    steps = uninstall(host=host, unit_dir=units, runtime_dir=runtime_dir)

    recorded = [list(argv) for argv in commands.argvs]
    reset = ["systemctl", "reset-failed", name]
    assert recorded.index(["systemctl", "daemon-reload"]) < recorded.index(reset)
    assert steps[-1] == f"cleared the failed record of {name}"
    (units / name).unlink()
    before_count = len(commands.argvs)
    assert uninstall(host=host, unit_dir=units, runtime_dir=runtime_dir) == [
        f"cleared the failed record of {name}"
    ]
    after = [list(argv) for argv in commands.argvs[before_count:]]
    assert after == [
        ["systemctl", "show", "--property=ActiveState", "--value", name],
        reset,
    ]


def test_uninstall_resets_nothing_it_did_not_leave_failed(
    ctx: BootUnitContext, tmp_path: Path
) -> None:
    units, runtime_dir, commands = _manager_reports(tmp_path, "inactive")
    assert uninstall(host=_linux(commands), unit_dir=units, runtime_dir=runtime_dir) == []
    assert [list(argv) for argv in commands.argvs] == [
        ["systemctl", "show", "--property=ActiveState", "--value", "meiva-boot.service"]
    ]


def test_privileged_translates_a_missing_sudo() -> None:
    commands = FakeCommands()
    commands.on("sudo", FileNotFoundError(2, "No such file or directory", "sudo"))

    with pytest.raises(RuntimeError, match="passwordless sudo"):
        boot_unit.privileged(["true"], host=fake_host(commands, uid=1000))


def test_privileged_runs_a_root_command_directly_and_others_through_sudo() -> None:
    commands = FakeCommands()
    commands.on(["true"], Reply())
    commands.on(["sudo"], Reply())
    boot_unit.privileged(["true"], host=fake_host(commands, uid=0))
    boot_unit.privileged(["true"], host=fake_host(commands, uid=1000))
    assert commands.argvs == [("true",), ("sudo", "-n", "true")]


def test_the_boot_unit_carries_no_home() -> None:
    assert boot_unit.UNIT_NAME == "meiva-boot.service"
    assert boot_unit.unit_path() == boot_unit.SYSTEM_UNIT_DIR / "meiva-boot.service"


def _write_cgroup(proc_root: Path, pid: int, cgroup_path: str) -> None:
    cgroup_file = proc_root / str(pid) / "cgroup"
    cgroup_file.parent.mkdir(parents=True, exist_ok=True)
    cgroup_file.write_text(f"0::{cgroup_path}\n")


def test_interactive_start_never_publishes(ctx: BootUnitContext, tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    _write_cgroup(proc_root, 555, "/user.slice/interactive.scope")
    commands = FakeCommands()
    boot_unit.publish_root_ready(
        ctx.home,
        OwnedProcess(123, 1.0, 1),
        host=_linux(commands),
        proc_root=proc_root,
        control_pid=555,
    )
    assert commands.argvs == []


@pytest.mark.parametrize("fault", ["dead", "foreign", "manager", "reused", "none"])
def test_root_publication_binds_native_custody(
    ctx: BootUnitContext, tmp_path: Path, fault: str
) -> None:
    expected = "/system.slice/meiva-boot.service"
    live_self = OwnedProcess.capture(psutil.Process(os.getpid()))
    owner = OwnedProcess(99999999, 1.0, 1) if fault == "dead" else live_self
    control_pid = 4242
    proc_root = tmp_path / "proc"
    _write_cgroup(proc_root, control_pid, expected)
    _write_cgroup(
        proc_root,
        owner.pid,
        "/foreign" if fault == "foreign" else expected,
    )
    commands = FakeCommands()
    commands.on(
        [
            "systemctl",
            "show",
            "--property=MainPID,ControlPID,ControlGroup,ActiveState",
            "meiva-boot.service",
        ],
        Reply(
            "MainPID=0\n"
            f"ControlPID={999 if fault == 'manager' else control_pid}\n"
            f"ControlGroup={expected}\n"
            "ActiveState=activating\n"
        ),
    )
    host = _linux(commands)
    path = boot_unit.root_pid_path(ctx.home)
    if fault == "none":
        path.parent.mkdir(parents=True)
        path.write_text("999999\n")
        boot_unit.publish_root_ready(
            ctx.home,
            owner,
            host=host,
            proc_root=proc_root,
            control_pid=control_pid,
        )
        assert path.read_text() == f"{owner.pid}\n"
        assert path.stat().st_mode & 0o777 == 0o600
    elif fault == "reused":
        path.parent.mkdir(parents=True)
        path.write_text(f"{owner.pid}\n")
        with pytest.raises(RuntimeError, match=r"custody|birth"):
            verify_post_publication(path, live=False, cgroup=expected)
        assert not path.exists()
    else:
        with pytest.raises(RuntimeError, match=r"custody|birth"):
            boot_unit.publish_root_ready(
                ctx.home,
                owner,
                host=host,
                proc_root=proc_root,
                control_pid=control_pid,
            )
        assert not path.exists()


def test_uninstall_failed_stop_preserves_unit(ctx: BootUnitContext, tmp_path: Path) -> None:
    target = boot_unit.unit_path(tmp_path)
    target.write_text(render_unit(ctx))
    commands = FakeCommands()
    commands.on(
        ["systemctl", "disable", "--now", "meiva-boot.service"],
        Reply(returncode=1, stderr="stop failed"),
    )
    with pytest.raises(RuntimeError, match="stop failed"):
        uninstall(
            host=_linux(commands),
            unit_dir=tmp_path,
            runtime_dir=tmp_path / "absent",
        )
    assert target.exists()
    assert [list(argv) for argv in commands.argvs] == [
        ["systemctl", "disable", "--now", "meiva-boot.service"]
    ]


def test_install_enables_native_unit_without_recursively_starting(
    ctx: BootUnitContext, tmp_path: Path
) -> None:
    commands, unit_dir, runtime_dir = _install_seams(tmp_path, enabled=False)
    steps = install(
        context=ctx,
        host=_linux(commands),
        unit_dir=unit_dir,
        runtime_dir=runtime_dir,
    )
    recorded = [list(argv) for argv in commands.argvs]
    assert recorded[0][:3] == ["install", "-m", "0644"]
    assert recorded[0][-1] == str(boot_unit.unit_path(unit_dir))
    assert ["systemctl", "enable", "meiva-boot.service"] in recorded
    assert ["systemctl", "start", "meiva-boot.service"] not in recorded
    assert not any("crontab" in arg for call in recorded for arg in call)
    assert "enabled meiva-boot.service" in steps


def test_unchanged_enabled_unit_never_restarts_or_rewrites(
    ctx: BootUnitContext, tmp_path: Path
) -> None:
    commands, unit_dir, runtime_dir = _install_seams(tmp_path, enabled=True)
    target = boot_unit.unit_path(unit_dir)
    target.parent.mkdir()
    target.write_text(render_unit(ctx))
    assert (
        install(
            context=ctx,
            host=_linux(commands),
            unit_dir=unit_dir,
            runtime_dir=runtime_dir,
        )
        == []
    )
    assert [list(argv) for argv in commands.argvs] == [
        ["systemctl", "is-enabled", "meiva-boot.service"]
    ]
