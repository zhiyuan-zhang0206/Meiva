import pytest

from base.agents.context.clients import DatabaseFactory
from base.telemetry.tests.capture.event_capture import EmittedEvents
from cli.commands.cluster.tests.health_probe_inputs import (
    emitted_events as emitted_events,
)
from cli.commands.cluster.tests.health_probe_inputs import (
    entry_command as entry_command,
)
from cli.commands.cluster.tests.health_probe_inputs import gateway as gateway
from cli.commands.cluster.tests.health_probe_inputs import probe_command as probe_command
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database


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
