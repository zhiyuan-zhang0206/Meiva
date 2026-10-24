# meiva:note
# What a held stop's deadline report says about the processes left, as values: the line for each
# survivor, the recovery advice for each way the stop ended, and the JSON-safe payload the
# lifecycle journal stores. Reading the live facts of a real process is asserted in
# `lifecycle/tests/stop/test_maintenance_stop_report.py`.

from __future__ import annotations

import dataclasses

from cli.commands.data_plane.maintenance.survivors import (
    StopSurvivor,
    SurvivorInventory,
    SurvivorRole,
)

_SHELL = StopSurvivor(
    pid=4242,
    role=SurvivorRole.SHELL,
    service="meiva-agent-1-shell-1",
    ppid=1,
    pgid=4242,
    status="sleeping",
    birth=1000.5,
    starttime=987654,
    cmdline="/bin/zsh -l",
)
_UNREADABLE = StopSurvivor(
    pid=77,
    role=SurvivorRole.JOB,
    service=None,
    ppid=None,
    pgid=None,
    status=None,
    birth=2000.0,
    starttime=None,
    cmdline=None,
)


def test_a_survivor_names_itself_with_every_fact_that_was_read() -> None:
    assert _SHELL.render() == (
        "    · pid=4242 ppid=1 pgid=4242 role=shell service='meiva-agent-1-shell-1' "
        "status=sleeping birth=1000.500 starttime=987654 cmdline='/bin/zsh -l'"
    )


def test_an_unreadable_survivor_is_listed_not_dropped() -> None:
    assert _UNREADABLE.render() == (
        "    · pid=77 role=job service=<unattributed> birth=2000.000 cmdline=<unreadable>"
    )


def test_a_long_command_line_is_cut_to_the_shown_length() -> None:
    long_one = dataclasses.replace(_SHELL, cmdline="x" * 500)

    shown = long_one.render().split("cmdline=")[1]

    assert shown == repr("x" * 297 + "...")


def test_the_inventory_of_a_stop_that_killed_nothing_offers_a_retry_or_force() -> None:
    text = SurvivorInventory((_SHELL,)).render(stage="shell-sessions")

    assert text.splitlines()[0] == (
        "remaining process inventory (stage=shell-sessions; nothing was force-killed):"
    )
    assert _SHELL.render() in text
    assert "retry the command to reconverge recorded groups" in text
    assert "meiva stop --mode force" in text


def test_the_inventory_of_survivors_of_a_sigkill_points_at_their_owner() -> None:
    text = SurvivorInventory((_SHELL, _UNREADABLE)).render(stage="shell-sessions", killed=True)

    assert "these outlived their SIGKILL" in text
    assert "another user's" in text and "end it as its owner" in text
    assert text.index("pid=4242") < text.index("pid=77")


def test_an_inventory_with_no_survivor_readable_says_so() -> None:
    assert SurvivorInventory(()).render(stage="data-plane") == (
        "remaining process inventory (stage=data-plane; nothing was force-killed): "
        "no survivor readable at the deadline"
    )


def test_the_payload_is_every_survivor_field_in_order() -> None:
    assert SurvivorInventory((_UNREADABLE,)).payload() == [
        {
            "pid": 77,
            "role": "job",
            "service": None,
            "ppid": None,
            "pgid": None,
            "status": None,
            "birth": 2000.0,
            "starttime": None,
            "cmdline": None,
        }
    ]
