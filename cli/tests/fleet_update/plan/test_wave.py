# meiva:note
# The order and the flags of a window's stops, and what the window does about a stop that failed:
# the plan's answers as values, with no host to ask.
# meiva:link cli/docs/fleet-update-window/failed-stop-recovery.meiva.okf.md
from __future__ import annotations

import pytest

from cli.fleet_update.plan.journal import (
    HostEvidence,
    Journal,
    parse_evidence,
    parse_journal,
    stop_completed,
    stop_failed,
)
from cli.fleet_update.plan.recovery import Recovery, recovery_action, recovery_report
from cli.fleet_update.plan.stop_wave import REBUILD_STOP, StopChoices, StopStep, stop_steps
from cli.fleet_update.plan.wave_order import start_order, stop_order

_CHOICES = StopChoices(
    forwarded=("--keep-shell-sessions", "--keep-browser", "--keep-extras", "--keep-no-services"),
    mode="smooth",
    timeout=600,
    infra="--keep-infra",
)


def test_the_gateway_starts_first_and_stops_last() -> None:
    assert start_order("gw", ("mac", "lin")) == ("gw", "mac", "lin")
    assert stop_order("gw", ("mac", "lin")) == ("mac", "lin", "gw")


def test_every_runner_stops_before_the_gateway_with_the_operators_flags_verbatim() -> None:
    steps = stop_steps(
        gateway="gw", runners=("mac", "lin"), rebuilding=frozenset(), choices=_CHOICES
    )
    forwarded = (
        "--keep-shell-sessions --keep-browser --keep-extras --keep-no-services "
        "--mode smooth --timeout 600"
    )
    assert steps == (
        StopStep("mac", forwarded, retires_extras=False),
        StopStep("lin", forwarded, retires_extras=False),
        StopStep("gw", f"{forwarded} --keep-infra", retires_extras=False),
    )


def test_a_rebuilding_host_retires_extras_and_the_gateway_keeps_its_data_plane_choice() -> None:
    steps = stop_steps(
        gateway="gw", runners=("mac",), rebuilding=frozenset({"mac", "gw"}), choices=_CHOICES
    )
    assert steps == (
        StopStep("mac", f"{REBUILD_STOP} --mode smooth --timeout 600", retires_extras=True),
        StopStep(
            "gw", f"{REBUILD_STOP} --mode smooth --timeout 600 --keep-infra", retires_extras=True
        ),
    )


def test_a_cluster_with_no_runner_stops_only_the_gateway() -> None:
    steps = stop_steps(gateway="gw", runners=(), rebuilding=frozenset(), choices=_CHOICES)
    assert [step.alias for step in steps] == ["gw"]


_STOP_OK = '{"operation": "stop", "complete": true, "result": {"rc": 0}}'


@pytest.mark.parametrize(
    ("text", "journal"),
    [
        (_STOP_OK, Journal("stop", complete=True, rc=0)),
        ('{"operation": "stop", "complete": true, "result": {"rc": 1}}', Journal("stop", True, 1)),
        ('{"operation": "stop", "complete": false}', Journal("stop", complete=False, rc=None)),
        ('{"operation": "restart"}', Journal("restart", complete=False, rc=None)),
        ('{"operation": "stop", "complete": "yes", "result": null}', Journal("stop", False, None)),
        (
            '{"operation": "stop", "complete": true, "result": {"rc": true}}',
            Journal("stop", True, None),
        ),
        ("", None),
        ("{not json", None),
        ("[1, 2]", None),
        ('"stop"', None),
    ],
)
def test_a_journal_is_read_without_trusting_it(text: str, journal: Journal | None) -> None:
    assert parse_journal(text) == journal


@pytest.mark.parametrize(
    ("journal", "completed", "failed"),
    [
        (Journal("stop", complete=True, rc=0), True, False),
        (Journal("stop", complete=True, rc=1), False, True),
        (Journal("stop", complete=False, rc=None), False, True),
        (Journal("stop", complete=True, rc=None), False, True),
        (Journal("restart", complete=True, rc=0), False, False),
        (Journal(None, complete=True, rc=0), False, False),
        (None, False, False),
    ],
)
def test_a_stop_is_complete_only_with_rc_zero_and_failed_only_if_it_is_a_stop_record(
    journal: Journal | None, completed: bool, failed: bool
) -> None:
    assert stop_completed(journal) is completed
    assert stop_failed(journal) is failed


def test_the_evidence_script_output_is_a_daemon_state_and_the_first_json_line() -> None:
    assert parse_evidence(f"host=up\n{_STOP_OK}") == HostEvidence(True, Journal("stop", True, 0))
    assert parse_evidence("host=down\n") == HostEvidence(False, None)
    assert parse_evidence("") == HostEvidence(False, None)
    assert parse_evidence("host=down\n{not json") == HostEvidence(False, None)


_DONE = HostEvidence(False, Journal("stop", complete=True, rc=0))
_FAILED = HostEvidence(False, Journal("stop", complete=True, rc=1))


@pytest.mark.parametrize(
    ("alias", "evidence", "expected"),
    [
        ("mac", HostEvidence(True, None), Recovery.RUNNING),
        ("mac", HostEvidence(True, Journal("stop", True, 1)), Recovery.RUNNING),
        ("mac", _DONE, Recovery.RESTART),
        ("lin", _FAILED, Recovery.RESTART),
        ("gw", _DONE, Recovery.LEFT_DOWN),
        ("gw", HostEvidence(False, None), Recovery.LEFT_DOWN),
    ],
)
def test_a_down_host_comes_back_only_when_this_run_took_it_down(
    alias: str, evidence: HostEvidence, expected: Recovery
) -> None:
    got = recovery_action(alias=alias, stopped=frozenset({"mac"}), failed="lin", evidence=evidence)
    assert got is expected


def _failed_host_action(evidence: HostEvidence) -> Recovery:
    return recovery_action(alias="lin", stopped=frozenset({"mac"}), failed="lin", evidence=evidence)


def test_the_failed_host_is_restarted_only_if_its_own_journal_records_a_failed_stop() -> None:
    assert _failed_host_action(HostEvidence(False, None)) is Recovery.LEFT_DOWN
    assert (
        _failed_host_action(HostEvidence(False, Journal("restart", True, 0))) is Recovery.LEFT_DOWN
    )
    assert (
        _failed_host_action(HostEvidence(False, Journal("stop", False, None))) is Recovery.RESTART
    )


def test_the_recovery_report_names_who_is_serving_and_who_needs_an_operator() -> None:
    assert recovery_report(("mac", "lin"), ("mac", "lin")) == (
        "recovered: mac, lin",
        "needs manual: none",
    )
    assert recovery_report(("mac", "lin"), ("mac",)) == ("recovered: mac", "needs manual: lin")
    assert recovery_report((), ()) == ("recovered: none", "needs manual: none")
