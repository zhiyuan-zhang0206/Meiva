from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from base.host.env.harness_binaries import require_harness_paths
from base.host.system.cron import health_job, mechanics
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import (
    DARWIN_ARM64,
    LINUX_X86,
    FakeCommands,
    Reply,
    fake_host,
)
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records

_UID = 501


@pytest.fixture()
def fake_home(tmp_path: Path) -> Path:
    return tmp_path


def _plant_plist(fake_home: Path, label: str) -> Path:
    agents = fake_home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    p = agents / f"{label}.plist"
    p.write_text("<plist/>")
    return p


def _assert_probe_plist(plist: Path, *, interval_s: int = 300) -> None:
    values = plistlib.loads(plist.read_bytes())
    assert values["Label"] == "com.meiva.health-probe"
    assert values["ProgramArguments"] == [
        str(Path(__file__).resolve().parents[5] / ".venv/bin/meiva"),
        "cluster",
        "health-probe",
    ]
    assert values["StartInterval"] == interval_s
    assert values["RunAtLoad"] is False


_NOT_LOADED = Reply(returncode=113, stderr="Could not find service")


def _launchctl(print_reply: Reply = _NOT_LOADED) -> tuple[FakeCommands, Host]:
    commands = FakeCommands()
    commands.on(["launchctl", "print"], print_reply, repeat=True)
    commands.on("launchctl", Reply(), repeat=True)
    return commands, fake_host(commands, platform=DARWIN_ARM64, uid=_UID)


def test_register_macos_never_reloads_its_own_launchd_job(
    fake_home: Path,
) -> None:
    label = "com.meiva.health-probe"
    plist = _plant_plist(fake_home, label)
    plist.write_text("<old-plist/>")
    commands, host = _launchctl()

    assert (
        health_job.register_macos(
            300,
            host=host,
            user_home=fake_home,
            current_label=label,
            ancestral_labels=frozenset(),
        )
        == 0
    )
    assert plist.read_text() == "<old-plist/>"
    assert commands.argvs == []


def test_register_macos_still_reloads_from_another_launchd_job(
    fake_home: Path,
) -> None:
    plist = _plant_plist(fake_home, "com.meiva.health-probe")
    plist.write_text("<old-plist/>")
    commands, host = _launchctl()

    assert (
        health_job.register_macos(
            300,
            host=host,
            user_home=fake_home,
            current_label="com.meiva.autostart",
            ancestral_labels=frozenset(),
        )
        == 0
    )
    _assert_probe_plist(plist)
    assert [argv[1] for argv in commands.argvs] == ["bootout", "bootstrap"]


def _crontab(
    existing: str = "",
    *,
    read_rc: int = 0,
    read_error: str = "",
    write_rc: int = 0,
    write_error: str = "",
) -> tuple[FakeCommands, Host]:
    commands = FakeCommands()
    commands.on(["crontab", "-l"], Reply(existing, read_error, read_rc), repeat=True)
    commands.on(["crontab", "-"], Reply("", write_error, write_rc), repeat=True)
    host = fake_host(
        commands,
        platform=LINUX_X86,
        programs={"crontab": "/usr/bin/crontab", "meiva": "/x/meiva"},
    )
    return commands, host


def _written(commands: FakeCommands) -> str | None:
    bodies = [str(call.kwargs["input"]) for call in commands.calls if call.argv == ("crontab", "-")]
    return bodies[-1] if bodies else None


def test_register_linux_aborts_when_crontab_read_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    commands, host = _crontab(read_rc=1, read_error="permission denied")

    assert health_job.register_linux(300, host=host, repo=tmp_path) == 1
    assert "avoid clobbering" in capsys.readouterr().err
    assert _written(commands) is None


def test_register_linux_treats_no_crontab_as_empty(tmp_path: Path) -> None:
    commands, host = _crontab(read_rc=1, read_error="no crontab for u")

    assert health_job.register_linux(300, host=host, repo=tmp_path) == 0
    assert "health-probe" in str(_written(commands))


def test_launchd_path_env_includes_brew_bin() -> None:
    path = mechanics.launchd_path_env(
        host=fake_host(programs={"brew": "/opt/homebrew/bin/brew"}),
        meiva_binary="/Users/x/.local/bin/meiva",
    )
    assert "/opt/homebrew/bin" in path
    assert "/Users/x/.local/bin" in path
    assert "/usr/bin" in path


def test_launchd_path_env_falls_back_without_brew() -> None:
    path = mechanics.launchd_path_env(
        host=fake_host(),
        meiva_binary="/Users/x/.local/bin/meiva",
    )
    assert "/opt/homebrew/bin" in path
    assert "/usr/local/bin" in path


# meiva:note
# The runner's launchd job starts every harness, and `claude` / `agy` install into the
# user-local bins; a job that reaches only the venv's own dir and brew cannot start them
# (#5491). Pin the user dirs even when `meiva` itself lives under the brew prefix.
def test_launchd_path_env_reaches_the_user_install_dirs() -> None:
    entries = mechanics.launchd_path_env(host=fake_host()).split(":")

    assert str(Path.home() / ".local/bin") in entries
    assert str(Path.home() / ".npm-global/bin") in entries


# meiva:note
# The producer contract: whatever this job writes must itself pass the check every unit
# PATH has to pass, so a future edit that narrows the list fails here rather than at the
# first harness spawn (#5495).
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def test_the_launchd_path_passes_the_unit_check() -> None:
    path = mechanics.launchd_path_env(host=fake_host(programs={"brew": "/opt/homebrew/bin/brew"}))

    assert require_harness_paths(path, what="the launchd job PATH") == path


def test_launchd_path_env_deduplicates() -> None:
    host = fake_host(programs={"brew": "/opt/homebrew/bin/brew"})
    assert (
        mechanics.launchd_path_env(
            host=host,
            meiva_binary="/opt/homebrew/bin/meiva",
        )
        .split(":")
        .count("/opt/homebrew/bin")
        == 1
    )


def test_register_linux_stamps_the_fixed_marker(tmp_path: Path) -> None:
    commands, host = _crontab()

    assert health_job.register_linux(300, host=host, repo=tmp_path) == 0
    (line,) = str(_written(commands)).splitlines()
    assert line.endswith("cluster health-probe # meiva-health-probe")
    assert "--auto-rollback" not in line
    assert "--threshold" not in line


def test_register_linux_keeps_unrelated_lines_and_reports_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    other_job = (
        "*/1 * * * * /x/meiva cluster watchdog-probe --role gateway  # meiva-watchdog-probe.gateway"
    )
    commands, host = _crontab(f"0 3 * * * backup\n{other_job}\n")

    assert health_job.register_linux(300, host=host, repo=tmp_path) == 0
    assert capsys.readouterr() == (
        "  . crontab entry added (every 5 min)\n",
        "",
    )
    body = str(_written(commands))
    assert "0 3 * * * backup" in body and other_job in body
    assert body.count("# meiva-health-probe") == 1


@pytest.mark.parametrize(
    ("read_rc", "read_error", "write_rc", "write_error", "expected_out", "expected_err"),
    [
        (
            1,
            "permission denied",
            0,
            "",
            "",
            "  * crontab -l failed (permission denied); skipping health-probe registration to avoid clobbering the crontab\n",
        ),
        (0, "", 1, "disk full", "", "  * crontab update failed: disk full\n"),
    ],
)
def test_register_linux_failure_messages_and_rc(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    read_rc: int,
    read_error: str,
    write_rc: int,
    write_error: str,
    expected_out: str,
    expected_err: str,
) -> None:
    commands, host = _crontab(
        read_rc=read_rc,
        read_error=read_error,
        write_rc=write_rc,
        write_error=write_error,
    )

    assert health_job.register_linux(300, host=host, repo=tmp_path) == 1
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == (expected_out, expected_err)
    assert (_written(commands) is not None) == (read_rc == 0)


def test_register_linux_missing_crontab_message_and_rc(capsys: pytest.CaptureFixture[str]) -> None:
    assert health_job.register_linux(300, host=fake_host(platform=LINUX_X86)) == 0
    assert capsys.readouterr() == (
        "  ! health probe cron: crontab not installed on this host (skipping); cluster runs without a health-probe cron\n",
        "",
    )


def test_unregister_linux_removes_only_the_probe_line() -> None:
    probe = "*/5 * * * * /x/meiva cluster health-probe # meiva-health-probe"
    commands, host = _crontab(f"{probe}\n0 3 * * * backup\n")

    assert health_job.unregister_linux(host=host) == 0
    assert probe not in str(_written(commands))
    assert "0 3 * * * backup" in str(_written(commands))


def test_unregister_linux_ignores_the_watchdog_probe_lines() -> None:
    probe_line = "*/1 * * * * /x/meiva cluster watchdog-probe --role gateway  # meiva-watchdog-probe.gateway.meiva-mine"
    commands, host = _crontab(f"{probe_line}\n")

    assert health_job.unregister_linux(host=host) == 0
    assert _written(commands) is None


@pytest.mark.parametrize(
    ("read_rc", "existing", "write_rc", "expected_out", "should_write"),
    [
        (1, "", 0, "  . no crontab to unregister\n", False),
        (0, "0 3 * * * backup\n", 0, "  . no Meiva health-probe entry found in crontab\n", False),
        (
            0,
            "*/5 * * * * /x/meiva cluster health-probe # meiva-health-probe\n",
            0,
            "  . crontab entry removed\n",
            True,
        ),
        (0, "*/5 * * * * /x/meiva cluster health-probe # meiva-health-probe\n", 1, "", True),
    ],
)
def test_unregister_linux_messages_and_success_gated_removal(
    capsys: pytest.CaptureFixture[str],
    read_rc: int,
    existing: str,
    write_rc: int,
    expected_out: str,
    should_write: bool,
) -> None:
    commands, host = _crontab(existing, read_rc=read_rc, write_rc=write_rc)

    assert health_job.unregister_linux(host=host) == 0
    assert capsys.readouterr() == (expected_out, "")
    assert (_written(commands) is not None) == should_write


def test_unregister_macos_removes_only_the_probe_plist(
    tmp_path: Path,
) -> None:
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    probe = agents / "com.meiva.health-probe.plist"
    autostart = agents / "com.meiva.autostart.plist"
    probe.write_text("<plist/>")
    autostart.write_text("<plist/>")

    commands, host = _launchctl()

    assert health_job.unregister_macos(host=host, user_home=tmp_path) == 0
    assert not probe.exists()
    assert autostart.exists()
    assert commands.argvs == [
        ("launchctl", "bootout", f"gui/{_UID}/com.meiva.health-probe"),
        ("launchctl", "print", f"gui/{_UID}/com.meiva.health-probe"),
    ]


def test_register_refused_for_worktree_checkout_against_prod_home(
    default_home: Path,
) -> None:
    prod_home = default_home / ".meiva"
    (prod_home / "source").mkdir(parents=True)
    commands, host = _launchctl()

    health_job.register_os_cron(
        enabled=True,
        host=host,
        repo=default_home / "Meiva" / ".worktrees" / "r4",
        user_home=default_home,
        ancestral_labels=frozenset(),
    )

    assert commands.argvs == []


def test_register_allowed_from_the_homes_own_source_checkout(
    default_home: Path,
) -> None:
    prod_home = default_home / ".meiva"
    (prod_home / "source").mkdir(parents=True)
    commands, host = _launchctl()

    health_job.register_os_cron(
        enabled=True,
        host=host,
        repo=prod_home / "source",
        user_home=default_home,
        ancestral_labels=frozenset(),
    )

    assert [argv[1] for argv in commands.argvs] == ["bootout", "bootstrap"]
    _assert_probe_plist(default_home / "Library" / "LaunchAgents" / "com.meiva.health-probe.plist")


def test_register_allowed_for_a_default_home_without_source(
    default_home: Path,
) -> None:
    commands, host = _launchctl()

    health_job.register_os_cron(
        enabled=True,
        host=host,
        repo=default_home / "dev-src",
        user_home=default_home,
        ancestral_labels=frozenset(),
    )

    assert [argv[1] for argv in commands.argvs] == ["bootout", "bootstrap"]
    _assert_probe_plist(default_home / "Library" / "LaunchAgents" / "com.meiva.health-probe.plist")


def test_register_macos_defers_when_ancestry_proves_the_job(
    fake_home: Path,
) -> None:
    label = "com.meiva.health-probe"
    plist = _plant_plist(fake_home, label)
    plist.write_text("<old-plist/>")
    commands, host = _launchctl()

    assert (
        health_job.register_macos(
            300,
            host=host,
            user_home=fake_home,
            current_label="0",
            ancestral_labels=frozenset({label}),
        )
        == 0
    )
    assert plist.read_text() == "<old-plist/>"
    assert commands.argvs == []


def test_register_macos_reloads_when_env_reads_zero_but_tree_is_external(
    fake_home: Path,
) -> None:
    plist = _plant_plist(fake_home, "com.meiva.health-probe")
    plist.write_text("<old-plist/>")
    commands, host = _launchctl()

    assert (
        health_job.register_macos(
            300,
            host=host,
            user_home=fake_home,
            current_label="0",
            ancestral_labels=frozenset(),
        )
        == 0
    )
    _assert_probe_plist(plist)
    assert [argv[1] for argv in commands.argvs] == ["bootout", "bootstrap"]


def test_unregister_macos_reports_a_job_launchd_still_holds(
    fake_home: Path, loguru_records: LogRecords
) -> None:
    probe = _plant_plist(fake_home, "com.meiva.health-probe")
    _commands, host = _launchctl(print_reply=Reply())

    assert health_job.unregister_macos(host=host, user_home=fake_home) == 1

    assert not probe.exists()
    [event] = loguru_records.events("launchd_retirement")
    assert event["level"].name == "ERROR"
    assert "com.meiva.health-probe" in event["message"]


# meiva:note
# The child uses only private files and the existing refusing/scripted command resource;
# HOME does not isolate the real launchctl namespace.
def _child(scenario: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home & <private>"
    home.mkdir()
    env = {
        "HOME": str(home),
        "MEIVA_HOME": str(home / ".meiva"),
        "PATH": "/usr/bin:/bin",
        "MEIVA_CONFIG_FETCH": "skip",
        "XPC_SERVICE_NAME": "com.meiva.health-probe" if scenario == "deferred" else "0",
    }
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", str(Path(__file__).with_name("child_definition.py")), scenario],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def test_definition_import_has_no_scheduler_resource_imports(tmp_path: Path) -> None:
    _child("cold", tmp_path)


def test_external_registration_reloads_even_when_content_is_unchanged(tmp_path: Path) -> None:
    _child("external", tmp_path)


def test_own_job_defers_before_capture_or_directory_creation(tmp_path: Path) -> None:
    _child("deferred", tmp_path)


def test_bootstrap_failure_keeps_its_nonzero_result_and_written_plist(tmp_path: Path) -> None:
    result = _child("failure", tmp_path)
    assert "launchctl bootstrap failed" in result.stderr
