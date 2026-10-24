from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from base.api_contracts.agent_contract import ShellKillMode
from services.upkeep.ttl_reaper import owner_text, shell_plan
from services.upkeep.ttl_reaper.shell_plan import ExpiredShell, Settlement

_EXPIRED = datetime(2026, 10, 20, 9, 30, tzinfo=UTC)


def _shell(agent: int, session: int, machine: str | None) -> ExpiredShell:
    return ExpiredShell(agent, session, machine, _EXPIRED, _EXPIRED - timedelta(hours=1))


def test_rows_group_by_machine_in_arrival_order() -> None:
    rows = [_shell(1, 1, "a"), _shell(2, 2, "b"), _shell(1, 3, "a"), _shell(3, 4, "b")]

    plan = shell_plan.plan_round(rows)

    assert [(b.machine, [s.session_id for s in b.shells]) for b in plan.batches] == [
        ("a", [1, 3]),
        ("b", [2, 4]),
    ]
    assert plan.unplaced == ()


@pytest.mark.parametrize("machine", [None, "", shell_plan.UNKNOWN_MACHINE])
def test_a_row_without_a_usable_machine_is_set_aside(machine: str | None) -> None:
    plan = shell_plan.plan_round([_shell(1, 1, machine), _shell(2, 2, "a")])

    assert [b.machine for b in plan.batches] == ["a"]
    assert [s.session_id for s in plan.unplaced] == [1]


def test_an_empty_round_has_nothing_to_do() -> None:
    plan = shell_plan.plan_round([])

    assert plan.batches == () and plan.unplaced == ()


def test_a_kill_that_interrupted_a_job_settles_as_interrupted() -> None:
    assert shell_plan.settle(
        {"mode": "killed", "interrupted": True, "name": "build"}
    ) == Settlement(ShellKillMode.KILLED, True, "build")


def test_an_idle_kill_and_an_absent_session_settle_silently() -> None:
    idle = shell_plan.settle({"mode": "killed", "interrupted": False})
    gone = shell_plan.settle({"mode": "absent"})

    assert idle == Settlement(ShellKillMode.KILLED, False, None)
    assert gone == Settlement(ShellKillMode.ABSENT, False, None)


@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"mode": None},
        {"mode": "later"},
        {"mode": "machine_absent"},
        {"mode": "killed"},
    ],
    ids=["empty", "null-mode", "unknown-mode", "transport-verdict", "kill-without-interrupted"],
)
def test_a_malformed_answer_does_not_settle(answer: dict[str, object]) -> None:
    assert shell_plan.settle(answer) is None


def test_a_runner_cannot_claim_machine_absence() -> None:
    assert shell_plan.settle({"mode": "machine_absent", "interrupted": False}) is None
    assert shell_plan.machine_absent_settlement().verdict == "machine_absent"


@pytest.mark.parametrize("mode", list(ShellKillMode))
def test_every_runner_verdict_keeps_its_own_name(mode: ShellKillMode) -> None:
    settled = shell_plan.settle({"mode": mode.value, "interrupted": False})

    assert settled is not None and settled.verdict == mode.value


def test_the_notice_names_the_session_the_expiry_and_the_ttl() -> None:
    shell = _shell(7, 3, "a")
    settlement = Settlement(ShellKillMode.KILLED, True, "build")

    text = shell_plan.reclaim_notice(
        shell, settlement, zone=ZoneInfo("UTC"), today=date(2026, 10, 20)
    )

    assert text == (
        "Shell session 'build' (id 3, agent 7) was reclaimed after its TTL expired at 09:30, "
        "TTL 1h, interrupting a running task."
    )


def test_an_unnamed_session_is_named_by_its_id() -> None:
    shell = _shell(7, 3, "a")

    text = shell_plan.reclaim_notice(
        shell,
        Settlement(ShellKillMode.KILLED, True, None),
        zone=ZoneInfo("UTC"),
        today=date(2026, 10, 20),
    )

    assert text is not None and text.startswith("Shell session 3 (agent 7) was reclaimed")


@pytest.mark.parametrize(
    "settlement",
    [
        Settlement(ShellKillMode.KILLED, False, "idle"),
        Settlement(ShellKillMode.ABSENT, False, None),
        shell_plan.machine_absent_settlement(),
    ],
    ids=["idle-kill", "absent", "machine-absent"],
)
def test_a_reap_that_interrupted_nothing_sends_no_notice(settlement: Settlement) -> None:
    assert (
        shell_plan.reclaim_notice(
            _shell(7, 3, "a"), settlement, zone=ZoneInfo("UTC"), today=date(2026, 10, 20)
        )
        is None
    )


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(3600, "1h"), (7200, "2h"), (1800, "30m"), (90, "90s"), (0, "0s"), (-61, "0s")],
)
def test_ttl_durations_read_compactly(seconds: float, text: str) -> None:
    assert owner_text.human_ttl(seconds) == text


def test_a_time_on_the_zones_today_carries_no_date() -> None:
    shanghai = ZoneInfo("Asia/Shanghai")
    at = datetime(2026, 10, 20, 15, 59, tzinfo=UTC)

    assert owner_text.wall_clock(at, zone=shanghai, today=date(2026, 10, 20)) == "23:59"


def test_a_time_on_another_day_of_the_zone_carries_the_date() -> None:
    shanghai = ZoneInfo("Asia/Shanghai")
    at = datetime(2026, 10, 20, 16, 5, tzinfo=UTC)

    assert owner_text.wall_clock(at, zone=shanghai, today=date(2026, 10, 20)) == "10-21 00:05"


def test_no_zone_renders_the_host_zone() -> None:
    at = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    host = at.astimezone(None)

    assert owner_text.wall_clock(at, zone=None, today=host.date()) == host.strftime("%H:%M")


def test_the_expired_notice_never_reads_as_a_reply() -> None:
    text = owner_text.notice_expired_message(4, "Ship the release?", "09:30")

    assert text.startswith('Notice #4 "Ship the release?" reached its expiry at 09:30')
    assert "without a response from the user" in text
    assert not text.startswith("Re:")


def test_a_shown_page_tells_its_owner_to_stop_the_server() -> None:
    shown = owner_text.page_expired_message("report", 3, shown_by_agent=True)
    served = owner_text.page_expired_message("report", 3, shown_by_agent=False)

    assert "Stop the page's HTTP server" in shown and "meiva.ui.show()" in shown
    assert "meiva.ui.serve()" in served and "Stop the page" not in served
