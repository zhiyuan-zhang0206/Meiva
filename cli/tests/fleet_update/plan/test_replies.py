# meiva:note
# How the window reads what the programs it ships answer: the stored-schedule check
# and the alert silence. Each reply is text a host printed, and each reading is a value: a
# reply that cannot be read costs a warning, never a crash and never a silent pass.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md
from __future__ import annotations

import pytest

from cli.fleet_update.plan.silence_replies import silence_command, silence_warnings
from cli.fleet_update.plan.stored_schedules import ScheduleCheck, schedule_check

_NEW = "0123456789abcdef0123"
_RED_ROW = (
    "RED id=12 name=hierarchy-worker missing=call-signature:L42 catch_up(): "
    "missing a required argument: 'triggers'"
)


def test_a_clean_check_says_nothing() -> None:
    reply = "RESULT ts=t checked=13 green=13 red=0 rc=0\nVERIFY_RC=0"
    assert schedule_check(reply, allow_red=False, new=_NEW) == ScheduleCheck((), None)


def test_a_verdict_reused_from_the_earlier_pass_is_said_and_does_not_refuse() -> None:
    reused = f"REUSED: the stored schedules cannot be read now; using the verdict for {_NEW}"
    check = schedule_check(f"{reused}\nVERIFY_RC=0", allow_red=False, new=_NEW)
    assert check == ScheduleCheck((reused,), None)


def test_a_host_that_could_not_ask_is_not_a_verdict() -> None:
    reply = "SKIPPED: abc is not fetched on this host (a dry run does not fetch)"
    assert schedule_check(reply, allow_red=False, new=_NEW) == ScheduleCheck(
        ("stored schedule scripts: not checked (see above)",), None
    )


def test_a_red_check_refuses_with_its_rows_and_the_two_ways_out() -> None:
    reply = f"RESULT ts=t checked=13 green=12 red=1 rc=1\n{_RED_ROW}\nVERIFY_RC=1"
    check = schedule_check(reply, allow_red=False, new=_NEW)
    assert check.notes == ()
    assert check.refusal is not None
    assert check.refusal.startswith(
        "refused: stored schedule scripts are red under 0123456789ab and would crash-loop after `up`:"
    )
    assert _RED_ROW in check.refusal
    assert "RESULT ts=t" not in check.refusal
    assert "meiva schedules update <id> --script-file" in check.refusal
    assert "--allow-red-schedules" in check.refusal


def test_a_red_check_the_operator_allowed_warns_and_lists_the_rows() -> None:
    check = schedule_check(f"{_RED_ROW}\nVERIFY_RC=1", allow_red=True, new=_NEW)
    assert check.refusal is None
    assert check.notes == (
        f"WARNING: stored schedule scripts red under 0123456789ab, continuing:\n  {_RED_ROW}",
    )


def test_a_check_that_could_not_run_is_unevaluable_and_refuses_like_a_red() -> None:
    reply = "TOOL-ERROR OperationalError: connection refused\nVERIFY_RC=2"
    check = schedule_check(reply, allow_red=False, new=_NEW)
    assert check.refusal is not None
    assert "unevaluable (check exited 2)" in check.refusal
    assert "TOOL-ERROR OperationalError: connection refused" in check.refusal


def test_a_check_with_no_verdict_line_is_unevaluable() -> None:
    check = schedule_check("garbage", allow_red=False, new=_NEW)
    assert check.refusal is not None
    assert "unevaluable (check exited ?)" in check.refusal
    assert "no RED line: see the output above" in check.refusal


def test_opening_the_silence_carries_its_length_and_comment_quoted() -> None:
    command = silence_command("open", hours=4.0, comment="fleet update to abc: planned `up` outage")
    assert command.endswith(
        " - open --hours 4 --comment 'fleet update to abc: planned `up` outage'"
    )
    assert command.startswith('H="$HOME/.meiva"; S="$H/source"')


def test_closing_the_silence_carries_nothing_else() -> None:
    assert silence_command("close", hours=None, comment="").endswith(
        ' "$S/.venv/bin/python" - close'
    )


def test_a_silence_needs_a_length_to_open_and_a_known_verb() -> None:
    with pytest.raises(ValueError, match="length"):
        silence_command("open", hours=None, comment="x")
    with pytest.raises(ValueError, match="unknown silence verb"):
        silence_command("extend", hours=1.0, comment="")


def test_a_silence_that_could_not_change_state_warns_in_the_programs_words() -> None:
    reply = (
        "SILENCE failed: GET /api/x: URLError\nSILENCE skipped: no Grafana admin credential\nother"
    )
    assert silence_warnings("open", reply) == (
        "WARNING: alert silence not open: failed: GET /api/x: URLError",
        "WARNING: alert silence not open: skipped: no Grafana admin credential",
    )
    assert silence_warnings("close", "SILENCE closed 1") == ()
