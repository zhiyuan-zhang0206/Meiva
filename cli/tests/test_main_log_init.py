"""The verbs that bring a unit up keep their loguru records.

The fresh executable prelude drops loguru's default handler, so a CLI process that
opens no sink discards every record it writes through loguru. On the start
path some warnings exist only there: a skipped pgvector pre-create, untracked
migration files that will not be applied. `meiva start`, `meiva restart`
and `meiva lgtm on|off` (every in-process `cmd_start`)
open the sinks `base.telemetry.process_logging.init_cli_process` gives: stderr, the unit's
`logs/cli-<verb>.log` and the event pipeline, without a `service_started`
row. Other verbs print to the caller's terminal and open none.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Literal

import pytest
import yaml

import cli.main as _main
from base import telemetry
from base.clock import Clock, ClockConfig
from base.clock.tests.manual_timers import ManualTimers
from base.cluster import postgres
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentityReader
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import DbConfig
from base.host.env.bootstrap import BootstrapFetchError
from base.host.env.dotenv_boot import PLACEHOLDER_DB_URL
from base.host.system.tests.fake_commands import DARWIN_ARM64, fake_host
from base.sessions.tests.refuse_helper import refuse_helper
from base.telemetry import EventPipeline
from cli.database import (
    OperatorDatabaseFactory,
    OperatorEventPipeline,
    open_operator_database,
    operator_database_factory,
    operator_event_pipeline,
)
from cli.database.runtime import CommandRuntime, UserSession
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime
from cli.parsers import build_parser
from cli.tests.bootstrap.init_identity import initialized_runner_home

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

_FIRST_START = Path(__file__).with_name("bootstrap") / "child_first_start.py"


# meiva:note
# A fresh process initializes a home, enters it as `meiva start` does and opens the
# start's sinks; the warning the lifecycle would write only through loguru survives.
def test_the_sinks_a_start_opens_keep_a_loguru_warning_in_stderr_and_the_log(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    env = {key: value for key, value in os.environ.items() if not key.startswith("MEIVA_")}
    env.update(MEIVA_HOME=str(home))
    # meiva:note
    # The child is this repository's own fixed probe script, run with the interpreter in
    # isolated mode.
    child = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-B", str(_FIRST_START)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert child.returncode == 0, child.stderr

    stderr = _ANSI.sub("", child.stderr)
    [warning] = [line for line in stderr.splitlines() if "[pgvector] pre-create skipped" in line]
    assert "WARNING" in warning and "OperationalError" not in warning
    # meiva:note
    # the connection error itself, not a `%s` placeholder
    assert "127.0.0.1" in warning
    assert "service started" not in stderr

    records = [
        json.loads(line)
        for line in (home / "logs/cli-start.log").read_text().splitlines()
        if line.strip()
    ]
    assert any("[pgvector] pre-create skipped" in json.dumps(r) for r in records)
    assert not any("service_started" in json.dumps(r) for r in records)


@pytest.mark.parametrize(
    ("argv", "name"),
    [
        (["restart"], "cli-restart"),
        (["lgtm", "on", "--all-services"], "cli-lgtm"),
        (["lgtm", "off", "--all-services"], "cli-lgtm"),
        (["status"], None),
        (["lgtm", "status"], None),
    ],
)
def test_only_the_verbs_that_bring_a_unit_up_open_sinks_before_dispatch(
    tmp_path: Path, cli_log_sinks: list[str], argv: list[str], name: str | None
) -> None:
    def dispatched(_args: argparse.Namespace) -> int:
        cli_log_sinks.append("dispatched")
        return 0

    args = _main.parse_command_args(argv)
    args.func = dispatched
    assert (
        _main.dispatch_parsed(
            args,
            argv,
            declared=tmp_path,
            checkout=_REPO_ROOT,
            producer_factory=lambda db, machine, home: OperatorEventPipeline.recording(
                db, machine=machine, home=home, log_sinks=cli_log_sinks
            ),
            open_database=open_operator_database,
        )
        == 0
    )

    assert cli_log_sinks == ([name, "dispatched"] if name else ["dispatched"])


def test_a_settings_failure_while_opening_sinks_keeps_its_actionable_message(
    tmp_path: Path, cli_log_sinks: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Opening the sinks builds Settings; a runner whose gateway is unreachable
    fails there with the same message the command itself would print."""
    args = _main.parse_command_args(["restart"])

    def fail_after_sink(_args: argparse.Namespace) -> int:
        assert cli_log_sinks == ["cli-restart"]
        raise BootstrapFetchError(f"could not fetch cluster config ({cli_log_sinks[0]})")

    args.func = fail_after_sink
    assert (
        _main.dispatch_parsed(
            args,
            ["restart"],
            declared=tmp_path,
            checkout=_REPO_ROOT,
            producer_factory=lambda db, machine, home: OperatorEventPipeline.recording(
                db, machine=machine, home=home, log_sinks=cli_log_sinks
            ),
            open_database=open_operator_database,
        )
        == 1
    )

    assert "could not fetch cluster config (cli-restart)" in capsys.readouterr().err


def test_main_hands_start_the_explicit_caller_child_owner_or_a_fresh_one(
    unit_home: Path,
) -> None:
    owners: list[list[subprocess.Popen[bytes]]] = []

    initialized_runner_home(unit_home, unit_home / "checkout")
    shared: list[subprocess.Popen[bytes]] = []
    for retained in (shared, None, None):
        args = _main.parse_command_args(["start", "--all-services"], retained_children=retained)
        bound_retained = args.func.keywords["retained_children"]

        def dispatched(
            _args: argparse.Namespace, _bound: list[subprocess.Popen[bytes]] = bound_retained
        ) -> int:
            owners.append(_bound)
            return 0

        args.func = dispatched
        assert (
            _main.dispatch_parsed(
                args,
                ["start", "--all-services"],
                declared=unit_home,
                checkout=_REPO_ROOT,
                producer_factory=operator_producer,
                open_database=open_operator_database,
            )
            == 0
        )
    assert owners[0] is shared
    assert owners[1] is not shared and owners[2] is not owners[1]


def test_main_hands_stop_the_explicit_caller_child_owner(tmp_path: Path) -> None:
    owners: list[list[subprocess.Popen[bytes]]] = []

    shared: list[subprocess.Popen[bytes]] = []
    full_stop = ["--yes", "--close-shell-sessions", "--stop-browser", "--stop-extras"]
    # meiva:note
    # a runner home
    full_stop += ["--keep-no-services", "--mode", "smooth", "--timeout", "60"]
    argv = ["stop", *full_stop]
    args = _main.parse_command_args(argv, retained_children=shared)
    bound_retained = args.func.keywords["retained_children"]

    def stopped(_args: argparse.Namespace) -> int:
        owners.append(bound_retained)
        return 0

    args.func = stopped
    assert (
        _main.dispatch_parsed(
            args,
            argv,
            declared=tmp_path,
            checkout=_REPO_ROOT,
            producer_factory=operator_producer,
            open_database=open_operator_database,
        )
        == 0
    )
    assert owners == [shared]


@pytest.mark.parametrize("verb", [["start"], ["lgtm", "on"], ["lgtm", "off"]])
def test_inspection_parser_cannot_launch_without_a_child_owner(verb: list[str]) -> None:
    args = build_parser().parse_args([*verb, "--all-services"])
    with pytest.raises(ValueError, match="caller-owned child retention"):
        args.func(args)


def test_logging_and_dispatch_borrow_the_same_operator_factory(
    tmp_path: Path, cli_log_sinks: list[str]
) -> None:
    factories: list[OperatorDatabaseFactory] = []
    machines: list[MachineIdentityReader] = []
    homes: list[Path] = []
    pipelines: list[EventPipeline] = []
    inputs: list[EventPipeline] = []

    def producer_factory(
        database_factory: OperatorDatabaseFactory,
        machine: MachineIdentityReader,
        home: Path,
    ) -> OperatorEventPipeline:
        factories.append(database_factory)
        machines.append(machine)
        homes.append(home)
        pipeline = telemetry.EventPipeline(writer=lambda _batch: None)
        pipelines.append(pipeline)
        owner = OperatorEventPipeline.wrapping(
            pipeline,
            database_factory=database_factory,
            machine=machine,
            home=home,
        )
        owner.log_sinks = cli_log_sinks
        return owner

    def dispatch(args: argparse.Namespace) -> int:
        assert args.database_factory is factories[-1]
        assert args.runtime.machine is machines[-1]
        assert args.runtime.home == homes[-1], "the producer and the runtime name one home"
        inputs.append(args.producer())
        return 0

    for _ in range(2):
        args = _main.parse_command_args(["restart"])
        args.func = dispatch
        assert (
            _main.dispatch_parsed(
                args,
                ["restart"],
                declared=tmp_path,
                checkout=_REPO_ROOT,
                producer_factory=producer_factory,
                open_database=open_operator_database,
            )
            == 0
        )

    assert cli_log_sinks == ["cli-restart", "cli-restart"]
    assert homes == [tmp_path.resolve(), tmp_path.resolve()]
    assert machines[0] is not machines[1]
    assert factories[0] is not factories[1]
    assert inputs == pipelines
    assert all(p.stopped for p in pipelines)


def _cold_report(
    name: Literal["schedule", "stop", "dashboard"],
    args: argparse.Namespace,
    path: Path,
) -> int:
    args.database_factory.adopt_login(PLACEHOLDER_DB_URL)
    if name == "schedule":
        from cli.commands.management import schedules_verify

        args.check_file = args.rows_file = None
        args.no_notify = False
        return schedules_verify.h_schedules_verify(args)
    if name == "stop":
        from cli.commands.data_plane.stop_waits import report_postgres_stop_escalation

        note = report_postgres_stop_escalation(
            postgres.Escalation(detail="fast shutdown timed out", killed=(42,)),
            producer=args.producer,
        )
        assert "fast shutdown timed out" in note
        return 0
    from cli.commands.converge.spec import ConvergeCtx
    from cli.commands.observability import lgtm_native
    from cli.commands.observability.plans.lgtm.install import platform_tag

    repo = _REPO_ROOT
    tag = platform_tag(DARWIN_ARM64.system, DARWIN_ARM64.machine)
    assert tag is not None
    native = path / "lgtm/native"
    native.mkdir(parents=True)
    versions = yaml.safe_load((repo / "deploy/lgtm/native/versions.yml").read_text())
    (native / "version-prometheus").write_text(versions["prometheus"]["version"])
    (native / "platform-prometheus").write_text(tag)
    args.runtime.config.set_field("lgtm_storage_dir", str(path / "storage"))
    destination = native / "config/provisioning/dashboards/meiva-ops-main.json"
    destination.parent.mkdir(parents=True)
    destination.write_text("previous dashboard")
    args.runtime.machine.set(role="observability-station")
    context = ConvergeCtx(
        repo=repo,
        meiva_home=path,
        roles=frozenset({"observability-station"}),
        config=args.runtime.config,
        machine=args.runtime.machine,
        database_factory=args.database_factory,
        producer=args.producer,
        services=frozenset({"prometheus"}),
        helper=refuse_helper,
        host=fake_host(platform=DARWIN_ARM64),
        session=UserSession(user_home=path, login_shell="/bin/zsh", interactive=False),
        timers=ManualTimers(),
        clock=Clock(
            ClockConfig("UTC", None, message_timestamp_weekday=False), timers=ManualTimers()
        ),
    )
    lgtm_native.ensure_lgtm_native_step(context)
    return 0


def operator_producer(
    database_factory: OperatorDatabaseFactory, machine: MachineIdentityReader, home: Path
) -> OperatorEventPipeline:
    """The producer the entry builds for a command: the operator pipeline over its factory."""
    return operator_event_pipeline(database_factory, machine=machine, home=home)


@pytest.mark.parametrize(
    ("name", "event", "result"),
    [
        ("schedule", "schedule_verify_failed", 2),
        ("stop", "postgres_stop_escalated", 0),
        ("dashboard", "lgtm_dashboard_render_failed", 0),
    ],
)
def test_cold_reporter_delivers_through_main_owned_pipeline(
    name: Literal["schedule", "stop", "dashboard"],
    event: str,
    result: int,
    tmp_path: Path,
) -> None:
    events: list[telemetry.Event] = []
    gates: list[ProcessDbGate] = []
    pipelines: list[telemetry.EventPipeline] = []
    factories: list[OperatorDatabaseFactory] = []

    # Handles are never dialed; the same real operator gate reaches work and writer.
    def open_database(
        config: DbConfig, gate: ProcessDbGate, direct_url: ClusterDirectUrl
    ) -> Database:
        gates.append(gate)
        return open_operator_database(config, gate, direct_url)

    def producer_factory(
        database_factory: OperatorDatabaseFactory,
        machine: MachineIdentityReader,
        home: Path,
    ) -> OperatorEventPipeline:
        factories.append(database_factory)

        def write(batch: list[telemetry.Event]) -> None:
            database_factory()
            events.extend(batch)

        pipeline = telemetry.EventPipeline(writer=write)
        pipelines.append(pipeline)
        return OperatorEventPipeline.wrapping(
            pipeline,
            database_factory=database_factory,
            machine=machine,
            home=home,
        )

    def dispatch(args: argparse.Namespace) -> int:
        args.database_factory()
        return _cold_report(name, args, tmp_path)

    args = _main.parse_command_args(["status"])
    args.func = dispatch
    before_process_pipeline = telemetry.process_state["pipeline"]
    assert (
        _main.dispatch_parsed(
            args,
            ["status"],
            declared=tmp_path,
            checkout=_REPO_ROOT,
            producer_factory=producer_factory,
            open_database=open_database,
        )
        == result
    )
    assert telemetry.process_state["pipeline"] is before_process_pipeline
    if name == "dashboard":
        destination = tmp_path / "lgtm/native/config/provisioning/dashboards/meiva-ops-main.json"
        reference = (
            _REPO_ROOT / "deploy/lgtm/config/grafana/provisioning/dashboards/meiva-ops-main.json"
        )
        assert destination.read_text() == reference.read_text()
    assert factories == [args.database_factory]
    assert len(pipelines) == 1 and pipelines[0].stopped
    assert [record.event_name for record in events] == [event]
    assert len(gates) >= 2 and all(gate is gates[0] for gate in gates)


def test_quiet_command_does_not_construct_pipeline(
    command_runtime: CommandRuntime, tmp_path: Path
) -> None:
    owner = operator_event_pipeline(
        operator_database_factory(
            local_host=command_runtime.machine.host,
            open_database=open_operator_database,
        ),
        machine=command_runtime.machine,
        home=tmp_path,
    )
    assert owner.close() is None

    producers: list[OperatorEventPipeline] = []

    def quiet(args: argparse.Namespace) -> int:
        producers.append(args.producer)
        return 0

    args = _main.parse_command_args(["status"])
    args.func = quiet
    assert (
        _main.dispatch_parsed(
            args,
            ["status"],
            declared=tmp_path,
            checkout=_REPO_ROOT,
            producer_factory=operator_producer,
            open_database=open_operator_database,
        )
        == 0
    )
    assert len(producers) == 1
    assert producers[0].close() is None


def test_unfinished_command_writer_remains_owned_until_join(
    command_runtime: CommandRuntime,
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def write(_batch: list[telemetry.Event]) -> None:
        entered.set()
        assert release.wait(5)

    pipeline = telemetry.EventPipeline(writer=write, batch_size=1)
    owner = OperatorEventPipeline.wrapping(
        pipeline,
        database_factory=operator_database_factory(
            local_host=command_runtime.machine.host,
            open_database=open_operator_database,
        ),
        machine=command_runtime.machine,
        home=tmp_path,
    )
    try:
        telemetry.emit("telemetry", "schedule_verify_failed", producer=owner)
        assert entered.wait(5)
        receipt = owner.close(timeout=0)
        assert receipt is not None
        assert receipt.status is telemetry.DrainStatus.UNFINISHED
        assert owner() is pipeline and not pipeline.stopped
    finally:
        release.set()
        receipt = owner.close()
        assert receipt is not None
        assert receipt.status is telemetry.DrainStatus.COMPLETED


@pytest.mark.parametrize("body_fails", [False, True])
def test_command_close_preserves_original_writer_error_and_primary(
    body_fails: bool,
    tmp_path: Path,
) -> None:
    primary = ValueError("command failed")
    cleanup = RuntimeError("writer failed")

    def write(_batch: list[telemetry.Event]) -> None:
        raise cleanup

    pipeline = telemetry.EventPipeline(writer=write)

    def producer_factory(
        database_factory: OperatorDatabaseFactory,
        machine: MachineIdentityReader,
        home: Path,
    ) -> OperatorEventPipeline:
        return OperatorEventPipeline.wrapping(
            pipeline,
            database_factory=database_factory,
            machine=machine,
            home=home,
        )

    def dispatch(args: argparse.Namespace) -> int:
        telemetry.emit("telemetry", "schedule_verify_failed", producer=args.producer)
        if body_fails:
            raise primary
        return 0

    args = _main.parse_command_args(["status"])
    args.func = dispatch
    with pytest.raises(ValueError if body_fails else RuntimeError) as caught:
        _main.dispatch_parsed(
            args,
            ["status"],
            declared=tmp_path,
            checkout=_REPO_ROOT,
            producer_factory=producer_factory,
            open_database=open_operator_database,
        )
    assert caught.value is (primary if body_fails else cleanup)
    assert pipeline.stopped
    with pytest.raises(RuntimeError) as repeated:
        args.producer.close(timeout=0)
    assert repeated.value is cleanup
    if body_fails:
        assert any("writer failed" in note for note in primary.__notes__)


# meiva:note
# The real `_h_status` handler body executes against the real parsed Namespace.
def test_status_handler_body_forwards_the_parsed_namespace(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = initialized_runner_home(tmp_path / "runner", _REPO_ROOT)
    args = _main.parse_command_args(["status"])
    assert (
        _main.dispatch_parsed(
            args,
            ["status"],
            declared=home,
            checkout=_REPO_ROOT,
            producer_factory=operator_producer,
            open_database=open_operator_database,
        )
        == 0
    )
    assert isinstance(args.runtime, CommandRuntime)
    assert isinstance(args.database_factory, OperatorDatabaseFactory)
    assert "agent-runner" in capsys.readouterr().out
