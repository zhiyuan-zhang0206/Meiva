# meiva:note
# Reading the cluster's roster through the hosts themselves: when a re-read is owed while
# heartbeats catch up, what an unlisted laptop may be, the name a host reports versus the ssh
# alias it was reached by, and every shape of reply that is not a roster.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md

from __future__ import annotations

from collections.abc import Callable

import pytest

from cli.fleet_update.plan.host_failures import PARTIAL_FAILURE_EXIT
from cli.fleet_update.session import Pacing
from cli.tests.fleet_update.fleet_update_cluster import Cluster


def test_the_roster_is_re_read_while_heartbeats_catch_up(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.roster_lag = 3
    assert run("up") == 0
    assert cluster.roster_reads == 4


def test_a_roster_that_never_agrees_fails_with_its_last_state(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.roster_lag = 10**6
    cluster.pacing = Pacing(roster_poll_s=0.25, lease_tick_s=0.25)
    assert run("up", "--roster-timeout", "1") == 3
    assert cluster.roster_reads >= 1
    out = capsys.readouterr().out
    assert "roster after 1s: offline ['gw', 'mac', 'lin']" in out
    # meiva:note
    # A roster that never agrees leaves every host unverified, and every host still gets a line
    # saying why: skipping the refresh as well would hide the one stage that still ran.
    assert "smoke" not in {k for k, _ in cluster.effects}
    assert "refresh" in {k for k, _ in cluster.effects}
    assert (
        len(
            [
                line
                for line in out.splitlines()
                if line.startswith("matrix:") and "failed at 'roster'" in line
            ]
        )
        == 3
    )


_LAPTOP_OFF = {"row": (False, False, None, None, True)}


def test_an_unlisted_offline_machine_is_reported_not_required(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.laptop = _LAPTOP_OFF
    assert run("up") == 0
    out = capsys.readouterr().out
    assert "roster: laptop is not listed, not checked: online=False head=None running=None" in out
    assert len(cluster.smoked) == 3 and set(cluster.smoked) == {"gw", "mac", "lin"}
    refreshes = [a for k, a in cluster.effects if k == "refresh"]
    assert len(refreshes) == 3 and set(refreshes) == {"gw", "mac", "lin"}
    assert "up complete: every listed machine runs" in out


def test_a_listed_machine_that_is_offline_fails(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.laptop = _LAPTOP_OFF
    cluster.silent.add("lin")
    cluster.pacing = Pacing(roster_poll_s=0.25, lease_tick_s=0.25)
    assert run("up", "--roster-timeout", "1") == 3
    out = capsys.readouterr().out
    assert "roster after 1s: offline ['lin']" in out
    assert "smoke" not in {k for k, _ in cluster.effects}
    assert (
        len(
            [
                line
                for line in out.splitlines()
                if line.startswith("matrix:") and "failed at 'roster'" in line
            ]
        )
        == 3
    )


def test_the_roster_name_comes_from_the_host_not_its_ssh_alias(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.names["mac"] = "mac-mini"
    assert run("up") == 0
    assert len(cluster.smoked) == 3 and set(cluster.smoked) == {"gw", "mac-mini", "lin"}


@pytest.mark.parametrize("reply", ["Traceback (most recent call last):", "", '{"name": "gw"}'])
def test_a_roster_reply_that_is_no_roster_fails_every_host_at_the_roster_without_a_traceback(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str], reply: str
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.roster_reply = reply
    assert run("up") == PARTIAL_FAILURE_EXIT
    out = capsys.readouterr().out
    assert out.count("failed at 'roster': the roster ") == 3 + 3
    assert "Traceback" not in out.replace(reply, "")
    assert "refresh" in {k for k, _ in cluster.effects}


def test_a_listed_host_with_no_roster_row_is_an_error(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.hidden.add("lin")
    assert run("up") == 3
    out = capsys.readouterr().out
    assert "no roster row for ['lin']" in out
    assert "smoke" not in {k for k, _ in cluster.effects}
    assert (
        len(
            [
                line
                for line in out.splitlines()
                if line.startswith("matrix:") and "failed at 'roster'" in line
            ]
        )
        == 3
    )
