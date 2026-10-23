# meiva:note
# What a stop does once the shell-session closure has answered, asserted on the plan.
#
# The closure signals the shells and reports the known processes that outlived it; the shell
# observes which are still alive. A closed session's owner is told before anything can fail, a
# surviving shell fails the stop, a surviving job is only a diagnostic.

from __future__ import annotations

import pytest

from cli.commands.lifecycle.stop.closure_plan import (
    AwaitNoSessions,
    FailShells,
    ReportJobLeftover,
    SurvivorFact,
    WriteNotices,
    forced_failure_message,
    job_leftover_line,
    plan_after_closure,
    shells_alive_header,
)


def shell(session: str, pid: int, *, alive: bool = True) -> SurvivorFact:
    return SurvivorFact(session=session, role="shell", pid=pid, alive=alive)


def job(session: str, pid: int, *, alive: bool = True) -> SurvivorFact:
    return SurvivorFact(session=session, role="job", pid=pid, alive=alive)


def test_a_clean_closure_notifies_the_owners_then_waits_for_the_service_table_to_empty() -> None:
    assert plan_after_closure((), forced=False) == (WriteNotices(), AwaitNoSessions())


def test_a_job_that_outlived_the_closure_is_reported_and_does_not_fail_the_stop() -> None:
    assert plan_after_closure((job("s-1", 4242),), forced=False) == (
        WriteNotices(),
        ReportJobLeftover("s-1", 4242),
        AwaitNoSessions(),
    )


def test_a_surviving_shell_fails_the_stop_after_its_owners_were_told() -> None:
    # meiva:note
    # The service no longer lists a closed session, so a retry could never record its notice.
    assert plan_after_closure((shell("s-2", 7), job("s-1", 4242)), forced=False) == (
        WriteNotices(),
        ReportJobLeftover("s-1", 4242),
        FailShells(sessions=("s-2",), pids=(7,)),
    )


def test_a_survivor_that_died_before_the_report_is_not_reported_and_does_not_fail_the_stop() -> (
    None
):
    survivors = (shell("gone", 7, alive=False), job("gone", 8, alive=False))
    assert plan_after_closure(survivors, forced=False) == (WriteNotices(), AwaitNoSessions())


def test_the_failure_names_every_live_shell_once_in_pid_order() -> None:
    survivors = (shell("b", 30), shell("a", 20), shell("b", 10), shell("c", 40, alive=False))
    failure = FailShells(sessions=("a", "b"), pids=(10, 20, 30))
    assert plan_after_closure(survivors, forced=False)[-1] == failure
    assert shells_alive_header(failure) == (
        "shell-session closure incomplete — known shells remain alive: [10, 20, 30] "
        "from sessions: ['a', 'b']"
    )


def test_a_forced_closure_tells_no_owner_and_waits_for_nothing() -> None:
    assert plan_after_closure((), forced=True) == ()
    assert plan_after_closure((job("s-1", 5),), forced=True) == (ReportJobLeftover("s-1", 5),)


def test_a_forced_closure_still_fails_on_a_surviving_shell() -> None:
    plan = plan_after_closure((shell("s-2", 7),), forced=True)
    failure = FailShells(sessions=("s-2",), pids=(7,))
    assert plan == (failure,)
    assert forced_failure_message(failure) == "force stop did not close shell sessions: ['s-2']"


def test_a_job_leftover_names_its_session_and_pid() -> None:
    assert job_leftover_line(ReportJobLeftover("meiva-agent-1-shell-1", 99)) == (
        "shell-session closure 'meiva-agent-1-shell-1': known job still running: pid=99; "
        "inspect the process before taking action"
    )


@pytest.mark.parametrize("forced", [False, True])
def test_only_shells_and_jobs_are_survivors(forced: bool) -> None:
    odd = SurvivorFact(session="s", role="helper", pid=1, alive=True)
    # meiva:note
    # Anything that is not a shell is a job: a diagnostic, never a gate.
    assert ReportJobLeftover("s", 1) in plan_after_closure((odd,), forced=forced)
