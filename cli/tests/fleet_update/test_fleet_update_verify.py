from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable
from typing import cast

import pytest

from cli.fleet_update.fleet_verify import roster
from cli.fleet_update.plan.host_failures import PARTIAL_FAILURE_EXIT
from cli.fleet_update.session import FailedError, Pacing, Session
from cli.tests.fleet_update.fleet_update_cluster import Cluster

# meiva:note
# One roster reply that never settles: the machine is listed and online, but its running commit
# differs from its head, so the wait has to look again.
_UNSETTLED_ROSTER = json.dumps(
    [
        {
            "name": "mac-mini",
            "online": True,
            "identity_mismatch": False,
            "head_sha": "a" * 40,
            "running_sha": "b" * 40,
            "serve_agent_runner": True,
        }
    ]
)


# meiva:note
# The verification answers for every host, whatever the others did: one machine's red smoke must
# not delete what the window learned about the rest, so the run still verifies them and still
# reports a line per host.
def test_one_red_smoke_still_verifies_and_reports_every_host(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.fail[("smoke", "mac")] = 1
    assert run("up") == PARTIAL_FAILURE_EXIT
    out = capsys.readouterr().out
    assert len(cluster.smoked) == 3 and set(cluster.smoked) == {"gw", "mac", "lin"}
    matrix = [line.partition("matrix: ")[2] for line in out.splitlines() if "matrix: " in line]
    assert [row.split()[0] for row in matrix] == ["gw", "mac", "lin"]
    assert matrix[0].endswith("serving") and matrix[2].endswith("serving")
    assert matrix[1].startswith("mac failed at 'smoke'")
    assert "plugins verify" in out and "packages refresh" in out


def test_a_host_that_never_started_is_reported_skipped_not_missing(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.fail[("start", "lin")] = 1
    assert run("up") == PARTIAL_FAILURE_EXIT
    out = capsys.readouterr().out
    matrix = [line.partition("matrix: ")[2] for line in out.splitlines() if "matrix: " in line]
    assert len(matrix) == 3
    assert matrix[0].startswith("gw ") and matrix[1].startswith("mac ")
    assert matrix[2].startswith("lin failed at 'start'")
    assert len(cluster.smoked) == 2 and set(cluster.smoked) == {"gw", "mac"}


def test_a_dry_run_says_the_helper_gate_was_not_evaluated(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    _, run = env
    assert run("run", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "helper gate: NOT evaluated" in out
    assert out.count("matrix: ") == 3 and "skipped: dry run" in out


# meiva:note
# A counter standing in for the process clock: every sleep is recorded and moves the counter, so a
# window of whole seconds costs no wall time.
class _Timers:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# meiva:note
# The one gateway answer the roster wait reads, with the pacing and the clock the session carries.
# It counts the reads and refuses to go on after `most_reads`: the window is two seconds of a fake
# clock, so a wait that stopped advancing its clock would otherwise poll for ever, and this suite
# has no per-test timeout to stop it.
class _Gateway:
    def __init__(self, reply: str, pacing: Pacing, timers: _Timers, *, most_reads: int) -> None:
        self.reply = reply
        self.pacing = pacing
        self.timers = timers
        self.most_reads = most_reads
        self.reads = 0
        self.said: list[str] = []

    def run(
        self, alias: str, command: str, *, effect: bool = True, stdin: str | None = None
    ) -> str:
        self.reads += 1
        if self.reads > self.most_reads:
            raise AssertionError("the roster wait never bounded its polls")
        return self.reply

    def say(self, line: str) -> None:
        self.said.append(line)


# meiva:note
# The roster wait is spent on the clock the session carries. A wait that read `time` for itself
# would record no sleep here and burn the same two seconds of wall time instead.
def test_the_roster_wait_is_spent_on_the_sessions_clock() -> None:
    timers = _Timers()
    session = cast(
        Session,
        _Gateway(
            _UNSETTLED_ROSTER, Pacing(roster_poll_s=0.5, lease_tick_s=0.5), timers, most_reads=5
        ),
    )
    args = argparse.Namespace(gateway="gw", roster_timeout=2, smoke_timeout=1, dry_run=False)
    started = time.monotonic()

    with pytest.raises(FailedError):
        roster(session, args, {"mac": "mac-mini"}, "stdin", expect_offline=frozenset())

    assert timers.sleeps == [0.5, 0.5, 0.5, 0.5]
    assert time.monotonic() - started < 1.0
