# meiva:note
# The Postgres liveness probe answers in three states, and the third one is not "down".
#
# `pg_running` stands behind both the start path (does a postmaster already own this port?) and
# the `meiva status` line, so a probe that cannot run must never read as a server that is not
# running: the first reading would start a second postmaster, the second would tell an operator
# that a healthy cluster is down. These cases pin the split, and pin the bound that makes it real
# — the program's own `-t` produces the "no response" answer, while the runner's bound only ever
# reaches a program that ignored it.

from __future__ import annotations

import subprocess

import pytest

from base.host.system.tests.fake_commands import (
    FakeCommands,
    Reply,
    fake_host,
    real_commands_host,
)
from cli.commands.data_plane.instance.probe import ProbeState, pg_isready_reading
from cli.commands.data_plane.pg_probe import PostmasterWait, launch_failure, pg_running

_PROGRAM = "pg_isready"
_TIMEOUT_S = 3.0


# meiva:note
# The case 405 asked for by name: a probe whose program does not exist must not answer "not
# running". A real exec of a path that is not there raises OSError, which is the reading that the
# server was never asked anything.
def test_a_probe_program_that_cannot_run_is_unanswered_and_never_a_stopped_server() -> None:
    reading = pg_running("/nonexistent/pg_isready", 5432, "127.0.0.1", host=real_commands_host())

    assert reading.state is ProbeState.UNANSWERED
    assert "pg_isready could not run" in reading.detail


# meiva:note
# Each `pg_isready` exit status is read as exactly what it says: 0 accepting, 1 refusing, 2 no
# response, 3 no attempt made. The last one is the probe itself not running, so it is unanswered
# rather than a server that is down; an exit the program does not document is unanswered too.
@pytest.mark.parametrize(
    ("returncode", "expected", "retryable"),
    [
        (0, ProbeState.RUNNING, True),
        (1, ProbeState.NOT_RUNNING, True),
        (2, ProbeState.NOT_RUNNING, True),
        (3, ProbeState.UNANSWERED, False),
        (74, ProbeState.UNANSWERED, True),
    ],
)
def test_each_isready_exit_status_keeps_its_meaning(
    returncode: int, expected: ProbeState, retryable: bool
) -> None:
    reading = pg_isready_reading(returncode)
    assert reading.state is expected
    assert reading.retryable is retryable


# meiva:note
# The program is given a bound of its own (`-t`) and the runner's bound is larger, so a server
# that is slow to answer is the program's answer ("no response") instead of a killed probe.
def test_the_runner_bound_leaves_the_program_room_to_answer_first() -> None:
    commands = FakeCommands()
    commands.on(_PROGRAM, Reply(returncode=2))

    reading = pg_running(
        _PROGRAM, 5432, "127.0.0.1", host=fake_host(commands=commands), timeout_s=_TIMEOUT_S
    )

    assert reading.state is ProbeState.NOT_RUNNING
    call = commands.calls[0]
    assert call.argv[0] == _PROGRAM
    assert call.argv[call.argv.index("-t") + 1] == "3"
    assert call.timeout > 3.0


# meiva:note
# A probe that had to be killed never answered: it is unanswered, with the reason, and the detail
# never claims the server is down.
def test_a_probe_that_outlives_the_runner_bound_is_unanswered() -> None:
    commands = FakeCommands()
    commands.on(_PROGRAM, subprocess.TimeoutExpired(_PROGRAM, _TIMEOUT_S))

    reading = pg_running(
        _PROGRAM, 5432, "127.0.0.1", host=fake_host(commands=commands), timeout_s=_TIMEOUT_S
    )

    assert reading.state is ProbeState.UNANSWERED
    assert "did not exit within" in reading.detail


# meiva:note
# 405's second reading of the same requirement, this time at the launch site: a probe that cannot
# run must refuse the readiness wait with its own words. Waiting it out would end in the start
# timeout's "did not become ready", which an operator reads as a slow postmaster — the diagnosis
# the third state exists to prevent.
def test_a_probe_that_cannot_run_refuses_the_readiness_wait_with_its_reason() -> None:
    with pytest.raises(RuntimeError, match="pg_isready could not run"):
        PostmasterWait(
            "/nonexistent/pg_isready",
            5432,
            "127.0.0.1",
            host=real_commands_host(),
            timeout_s=_TIMEOUT_S,
        )


# meiva:note
# The other half: an unanswered probe that may still answer is "not ready yet", never a refusal —
# a postmaster that has not finished starting must not be reported as a broken probe. The wait
# still remembers that it never heard an answer, which is what the failure report turns on.
def test_an_unanswered_probe_that_may_still_answer_reads_as_not_ready() -> None:
    commands = FakeCommands()
    commands.on(_PROGRAM, subprocess.TimeoutExpired(_PROGRAM, _TIMEOUT_S), repeat=True)

    wait = PostmasterWait(
        _PROGRAM, 5432, "127.0.0.1", host=fake_host(commands=commands), timeout_s=_TIMEOUT_S
    )

    assert wait() is False
    assert wait.answered is False
    assert "did not exit within" in wait.last_unanswered


def test_a_running_postmaster_makes_the_readiness_wait_answer() -> None:
    commands = FakeCommands()
    commands.on(_PROGRAM, Reply(returncode=0), repeat=True)

    wait = PostmasterWait(
        _PROGRAM, 5432, "127.0.0.1", host=fake_host(commands=commands), timeout_s=_TIMEOUT_S
    )

    assert wait() is True
    assert wait.answered is True


# meiva:note
# 405's acceptance, second half: a postmaster that simply did not come up keeps the custody
# boundary's message byte-for-byte. The probe answered all along, so it has nothing to add.
def test_a_postmaster_that_did_not_start_keeps_its_message_verbatim() -> None:
    commands = FakeCommands()
    commands.on(_PROGRAM, Reply(returncode=2), repeat=True)
    message = "PostgreSQL did not become ready; captured custody is retained"
    wait = PostmasterWait(
        _PROGRAM, 5432, "127.0.0.1", host=fake_host(commands=commands), timeout_s=_TIMEOUT_S
    )

    assert launch_failure(wait, RuntimeError(message)) == message


# meiva:note
# 312's observation, made a rule by 405: a probe that answered earlier (here "not running") and is
# then killed on its last call must NOT get the "the probe has no answer" suffix — we did learn
# about the server, and a slow postmaster must keep reading as one.
def test_an_earlier_answer_keeps_the_message_verbatim_when_the_last_probe_is_killed() -> None:
    commands = FakeCommands()
    commands.on(
        _PROGRAM,
        Reply(returncode=2),
        subprocess.TimeoutExpired(_PROGRAM, _TIMEOUT_S),
        repeat=True,
    )
    message = "PostgreSQL did not become ready; captured custody is retained"
    wait = PostmasterWait(
        _PROGRAM, 5432, "127.0.0.1", host=fake_host(commands=commands), timeout_s=_TIMEOUT_S
    )

    assert wait() is False
    assert wait.answered is True
    assert launch_failure(wait, RuntimeError(message)) == message


# meiva:note
# And its first half: when the probe never answered, the failure carries the probe's own words, so
# an operator reads "the probe could not run" instead of "the server did not start".
def test_a_failed_start_names_the_probe_that_never_answered() -> None:
    commands = FakeCommands()
    commands.on(_PROGRAM, subprocess.TimeoutExpired(_PROGRAM, _TIMEOUT_S), repeat=True)
    silent = PostmasterWait(
        _PROGRAM, 5432, "127.0.0.1", host=fake_host(commands=commands), timeout_s=_TIMEOUT_S
    )
    assert silent() is False

    described = launch_failure(
        silent, RuntimeError("PostgreSQL did not become ready; captured custody is retained")
    )

    assert "did not become ready" in described
    assert "the probe has no answer" in described
    assert "pg_isready did not exit" in described
