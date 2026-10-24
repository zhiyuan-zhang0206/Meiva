# meiva:note
# How the window reads what the programs it ships answer: the stored-schedule check, the lease
# table and the alert silence. Each reply is text a host printed, and each reading is a value: a
# reply that cannot be read costs a warning, never a crash and never a silent pass.
# meiva:link cli/docs/fleet-update-window/lease-announcement.meiva.okf.md
from __future__ import annotations

import json

import pytest

from cli.fleet_update.plan.lease_announcement import (
    CountdownTick,
    Lease,
    LeaseReading,
    announcement,
    countdown,
    countdown_line,
    dry_run_line,
    read_leases,
    unread_warning,
)
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


_LEASE = {
    "agent_id": 6161,
    "session_id": 4,
    "executor": "Codex: db-work",
    "expires_at": "2026-10-19T23:10:00+00:00",
}


def test_the_leases_are_the_last_json_line_the_program_printed() -> None:
    reply = "a warning\n" + json.dumps({"leases": [_LEASE]}) + "\n"
    assert read_leases(reply) == LeaseReading(
        (Lease(6161, 4, "Codex: db-work", "2026-10-19T23:10:00+00:00"),), None
    )
    assert read_leases(json.dumps({"leases": []})) == LeaseReading((), None)


@pytest.mark.parametrize(
    ("reply", "detail"),
    [
        (
            "LEASES failed: OperationalError: connection refused",
            "LEASES failed: OperationalError: connection refused",
        ),
        ("no json here at all", "no output"),
        ("", "no output"),
        ('{"leases": "nope"}', "no output"),
        ("{not json", "no output"),
        ('{"other": 1}', "no output"),
        (json.dumps({"leases": [{"agent_id": 1}]}), "no output"),
        (json.dumps({"leases": [{**_LEASE, "agent_id": "x"}]}), "no output"),
    ],
)
def test_a_reply_that_is_no_lease_list_is_unread_with_the_programs_own_words(
    reply: str, detail: str
) -> None:
    assert read_leases(reply) == LeaseReading((), detail)


def test_an_unread_list_is_one_warning_that_says_what_goes_unannounced() -> None:
    assert unread_warning("boom") == (
        "WARNING: active impersonation leases were not read (boom); "
        "a takeover this window ends goes unannounced"
    )


def test_no_lease_is_one_line_and_each_lease_names_its_agent_session_and_expiry() -> None:
    assert announcement(()) == ("leases: no active impersonation lease",)
    lines = announcement((Lease(6161, 4, "Codex: db-work", "2026-10-19T23:10:00+00:00"),))
    assert lines[0] == "leases: 1 active impersonation lease(s):"
    assert lines[1] == (
        "leases:   agent 6161 session 4 executor 'Codex: db-work' expires 2026-10-19T23:10:00+00:00"
    )
    assert lines[2].startswith("leases: the stop ends every one of them")
    assert len(lines) == 3


def test_the_countdown_sleeps_a_tick_at_a_time_and_the_last_one_short() -> None:
    assert countdown(12, 5.0) == (
        CountdownTick(12.0, 5.0),
        CountdownTick(7.0, 5.0),
        CountdownTick(2.0, 2.0),
    )
    ticks = countdown(30, 5.0)
    assert [tick.left_s for tick in ticks] == [30, 25, 20, 15, 10, 5]
    assert sum(tick.sleep_s for tick in ticks) == 30


def test_a_window_of_zero_seconds_has_no_countdown() -> None:
    assert countdown(0, 5.0) == ()


def test_a_countdown_that_never_advances_is_refused() -> None:
    with pytest.raises(ValueError, match="positive"):
        countdown(10, 0)


def test_a_tick_line_and_the_dry_run_line() -> None:
    line = countdown_line(CountdownTick(25.0, 5.0))
    assert line == "leases: 25s left: release or renew them, or let the stop end them"
    assert dry_run_line(30) == "leases: dry run: not waiting 30s"


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
