from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from base.host.system import boot_policy
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from cli import start_intent
from cli.database import OperatorDatabaseFactory, operator_event_pipeline
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime
from cli.parsers import build_parser
from cli.start_runtime import HomeSite


# meiva:note
# One boot-job attempt whose start exited with `code`.
def _boot(home: Path, code: int) -> int:
    return start_intent.settle_boot_attempt(home, boot_policy.begin_attempt(home), code)


def test_the_parser_marks_a_boot_start_only_when_asked() -> None:
    parser = build_parser()
    assert parser.parse_args(["start", "--all-services"]).boot is False
    assert parser.parse_args(["start", "--all-services", "--boot"]).boot is True


def test_failed_attempts_before_the_budget_keep_their_exit_code(
    tmp_path: Path, emitted_events: EmittedEvents
) -> None:
    codes = [_boot(tmp_path, 75) for _ in range(boot_policy.BOOT_MAX_ATTEMPTS - 1)]
    assert codes == [75] * (boot_policy.BOOT_MAX_ATTEMPTS - 1)
    assert len(emitted_events) == 0


def test_the_attempt_that_spends_the_budget_alerts_and_takes_the_policys_exit_code(
    tmp_path: Path, emitted_events: EmittedEvents, capsys: pytest.CaptureFixture[str]
) -> None:
    for _ in range(boot_policy.BOOT_MAX_ATTEMPTS - 1):
        assert _boot(tmp_path, 75) == 75
    # meiva:note
    # launchd retries every nonzero exit, so the spending attempt exits 0 there; systemd
    # keeps the real code. The policy owns which, and tests it.
    assert _boot(tmp_path, 75) == boot_policy.exit_code_after_budget(75)
    (event,) = emitted_events
    assert (event.category, event.event_name) == ("telemetry", "boot_retries_exhausted")
    assert event.level == "error"
    assert event.attributes == {"attempts": boot_policy.BOOT_MAX_ATTEMPTS, "exit_code": 75}
    assert "not retrying" in capsys.readouterr().err


def test_a_start_after_the_budget_still_runs_and_success_clears_the_count(
    tmp_path: Path, emitted_events: EmittedEvents
) -> None:
    for _ in range(boot_policy.BOOT_MAX_ATTEMPTS):
        _boot(tmp_path, 75)
    # meiva:note
    # The count never refuses a start: this attempt is attempt number budget + 1, and it succeeds.
    assert _boot(tmp_path, 0) == 0
    assert boot_policy.begin_attempt(tmp_path) == 1


def _start_args(command_runtime: CommandRuntime, *flags: str) -> argparse.Namespace:
    args = build_parser().parse_args(["start", "--all-services", *flags])
    args.runtime = command_runtime
    return args


def _run_start(
    site: HomeSite,
    command_runtime: CommandRuntime,
    operator_database: OperatorDatabaseFactory,
    *flags: str,
) -> int:
    producer = operator_event_pipeline(
        operator_database, machine=command_runtime.machine, home=site.home
    )
    return start_intent.run_start(
        _start_args(command_runtime, *flags),
        site,
        retained_children=[],
        database_factory=operator_database,
        producer=producer,
    )


# meiva:note
# The homes were never initialized, so every start is refused (exit 1): the record
# of attempts is what tells a boot start from a plain one.
def test_a_boot_start_counts_its_attempt_and_a_plain_start_does_not(
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    command_runtime: CommandRuntime,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plain, boot = tmp_path / "plain", tmp_path / "boot"
    for home in (plain, boot):
        home.mkdir(mode=0o700)

    assert _run_start(HomeSite(plain, tmp_path), command_runtime, operator_database) == 1
    assert _run_start(HomeSite(boot, tmp_path), command_runtime, operator_database, "--boot") == 1

    assert "not initialized" in capsys.readouterr().err
    assert boot_policy.begin_attempt(plain) == 1, "a plain start recorded no attempt"
    assert boot_policy.begin_attempt(boot) == 2, "a boot start recorded its attempt"


def test_a_boot_start_after_the_budget_still_runs(
    tmp_path: Path,
    operator_database: OperatorDatabaseFactory,
    command_runtime: CommandRuntime,
    capsys: pytest.CaptureFixture[str],
) -> None:
    site = HomeSite(tmp_path / "home", tmp_path)
    site.home.mkdir(mode=0o700)
    for _ in range(boot_policy.BOOT_MAX_ATTEMPTS):
        _run_start(site, command_runtime, operator_database, "--boot")
    capsys.readouterr()

    _run_start(site, command_runtime, operator_database, "--boot")

    assert "not initialized" in capsys.readouterr().err, "the start itself ran"
