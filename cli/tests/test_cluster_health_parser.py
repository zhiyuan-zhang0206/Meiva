import io
import plistlib
from dataclasses import replace
from pathlib import Path

import pytest

from base.agents.context.clients import DatabaseFactory
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import (
    DARWIN_ARM64,
    LINUX_X86,
    FakeCommands,
    Reply,
    fake_host,
)
from base.host.system.tests.fake_crontab import FakeCrontab
from base.telemetry.tests.capture.event_capture import EmittedEvents
from cli import start_identity
from cli.commands.cluster.tests.health_probe_inputs import (
    emitted_events as emitted_events,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    entry_command as entry_command,
)
from cli.commands.cluster.tests.health_probe_inputs import gateway as gateway
from cli.commands.cluster.tests.health_probe_inputs import probe_command as probe_command
from cli.database.runtime import CommandRuntime, Terminal
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime
from cli.parsers import build_parser
from cli.startup.tests.birth_facts import birth_facts


@pytest.mark.parametrize(("flags", "schema_line"), [([], True), (["--no-schema-check"], False)])
def test_the_schema_flag_reaches_the_probe_the_command_runs(
    entry_command: CommandRuntime,
    operator_database: DatabaseFactory,
    emitted_events: EmittedEvents,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
    schema_line: bool,
) -> None:
    from cli.parsers import parse_args

    args = parse_args(["cluster", "health-probe", *flags])
    args.database_factory = operator_database
    args.producer = emitted_events.producer
    args.runtime = entry_command

    rc = args.func(args)

    out = capsys.readouterr().out
    assert rc in (0, 1)
    assert "  ✓ gateway liveness" in out
    assert ("  ✓ schema health" in out) is schema_line


@pytest.mark.parametrize(
    ("flag", "destination"),
    [
        ("schema-check", "schema_check"),
    ],
)
def test_health_probe_check_flags_disable_only_when_negated(flag: str, destination: str) -> None:
    from cli.parsers import build_parser

    parser = build_parser()
    default = parser.parse_args(["cluster", "health-probe"])
    disabled = parser.parse_args(["cluster", "health-probe", f"--no-{flag}"])
    assert getattr(default, destination) is True
    assert getattr(disabled, destination) is False
    with pytest.raises(SystemExit) as refused:
        parser.parse_args(["cluster", "health-probe", f"--{flag}"])
    assert refused.value.code == 2


# meiva:note
# The `meiva cluster` verbs that register or retire OS jobs, as the operator runs them: the real
# parser, a runtime whose home, checkout and account home are three different directories, and a
# scheduler that records what it was asked. The three are all paths, so a swap inside a verb's
# handler is not a type error; here it shows as a job that is not written or not removed, or one
# that names the wrong binary or home.
def _checkout(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "checkout"
    binary = root / ".venv" / "bin" / "meiva"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    return root, binary


def _runtime(
    command_runtime: CommandRuntime, tmp_path: Path, host: Host
) -> tuple[CommandRuntime, Path, Path]:
    account = tmp_path / "account"
    checkout, binary = _checkout(tmp_path)
    runtime = replace(
        command_runtime,
        home=account / ".meiva",
        checkout=checkout,
        host=host,
        session=replace(command_runtime.session, user_home=account),
    )
    runtime.config.set_field("os_jobs_enabled", True)
    return runtime, account, binary


def _run(runtime: CommandRuntime, *argv: str) -> int:
    args = build_parser().parse_args(["cluster", *argv])
    args.runtime = runtime
    return args.func(args)


def test_the_health_probe_verbs_anchor_the_crontab_job_to_the_commands_roots(
    command_runtime: CommandRuntime, tmp_path: Path
) -> None:
    crontab = FakeCrontab()
    host = fake_host(crontab, platform=LINUX_X86, programs={"crontab": "/usr/bin/crontab"})
    runtime, _account, binary = _runtime(command_runtime, tmp_path, host)

    assert _run(runtime, "health-probe-register") == 0
    assert crontab.lines == [
        f"*/5 * * * * MEIVA_HOME={runtime.home} {binary} cluster health-probe # meiva-health-probe"
    ]

    assert _run(runtime, "health-probe-unregister") == 0
    assert crontab.table.strip() == ""


def test_the_health_probe_verbs_anchor_the_launchd_job_to_the_commands_roots(
    command_runtime: CommandRuntime, tmp_path: Path
) -> None:
    launchctl = FakeCommands()
    launchctl.on(["launchctl", "print"], Reply(returncode=113), repeat=True)
    launchctl.on("launchctl", Reply(), repeat=True)
    host = fake_host(launchctl, platform=DARWIN_ARM64, uid=501)
    runtime, account, binary = _runtime(command_runtime, tmp_path, host)
    plist = account / "Library" / "LaunchAgents" / "com.meiva.health-probe.plist"

    assert _run(runtime, "health-probe-register") == 0
    values = plistlib.loads(plist.read_bytes())
    assert values["ProgramArguments"][0] == str(binary)
    assert values["EnvironmentVariables"]["MEIVA_HOME"] == str(runtime.home)

    _run(runtime, "health-probe-unregister")
    assert not plist.exists()


# meiva:note
# A stream that says it is a terminal, as `sys.stdin` and `sys.stdout` do for a person at one.
class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


# meiva:note
# The account's default home owns its OS jobs only when the verb hands the destroy that account's
# home: with the account home and the checkout interchanged the scheduler is never asked to clear
# them.
def test_the_destroy_verb_retires_the_jobs_of_the_account_the_command_runs_as(
    command_runtime: CommandRuntime, tmp_path: Path
) -> None:
    scheduler = FakeCommands()
    scheduler.on(lambda _argv: True, Reply(), repeat=True)
    runtime, account, _binary = _runtime(command_runtime, tmp_path, fake_host(scheduler))
    prod = runtime.home
    prod.mkdir(parents=True, mode=0o700)
    start_identity.prepare_identity(
        start_identity.IdentityInput(
            prod,
            runtime.checkout,
            frozenset({"gateway"}),
            {"MEIVA_MACHINE_NAME": "destroyed", "MEIVA_GATEWAY_URL": "http://10.0.0.7:8000"},
            occupied_ports=frozenset(),
            birth=birth_facts(),
        )
    )
    runtime = replace(
        runtime,
        terminal=Terminal(_Tty(f"{prod.resolve()}\n"), _Tty(), io.StringIO()),
        session=replace(runtime.session, user_home=account),
    )

    assert _run(runtime, "destroy", "--stop-mode", "smooth", "--stop-timeout", "300") == 0

    assert (prod / "destroy-intent.json").read_text().count('"detached"') == 1
    assert ("crontab", "-l") in scheduler.argvs
