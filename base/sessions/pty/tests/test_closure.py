"""Known-target closure is bounded and does not discover arbitrary descendants."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Iterator

import psutil
import pytest

from base.clock import SYSTEM_TIMERS
from base.clock.tests.manual_timers import ManualTimers
from base.native_process.ownership import OwnedProcess
from base.native_process.process_control import SYSTEM_PROCESS_CONTROL
from base.native_process.tests.fake_process_control import FakeProcessControl, Signalled
from base.sessions.pty import closure, process_groups
from base.sessions.pty.survivor_role import SurvivorRole

# meiva:note
# The test process names itself and its group the way a service root would; a scripted control
# answers for them too, because closure asks which group the caller is in.
OWN_PID = os.getpid()
OWN_GROUP = {OWN_PID: os.getpgrp()}


@pytest.fixture
def leaders() -> Iterator[list[subprocess.Popen[bytes]]]:
    started: list[subprocess.Popen[bytes]] = []
    yield started
    for process in started:
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)


def _session(leaders: list[subprocess.Popen[bytes]]) -> closure.Target:
    process = subprocess.Popen(["sleep", "300"], start_new_session=True)
    leaders.append(process)
    return closure.Target(
        "meiva-test-best-effort", OwnedProcess.capture(psutil.Process(process.pid))
    )


def test_idle_shell_closes_without_claiming_all_descendants_gone(
    leaders: list[subprocess.Popen[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _session(leaders)

    def no_scan(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("shell-session closure must not enumerate the host")

    with monkeypatch.context() as patch:
        patch.setattr(psutil, "process_iter", no_scan)
        patch.setattr(psutil, "pids", no_scan)
        outcome = closure.close_sessions(
            [target],
            grace_s=0.2,
            kill_s=0.2,
            process_control=SYSTEM_PROCESS_CONTROL,
            own_pid=OWN_PID,
            timers=SYSTEM_TIMERS,
        )
    assert outcome == closure.Outcome(grace_s=0.2)
    assert not target.shell.live()


def test_unknown_independent_process_is_left_to_operations(
    leaders: list[subprocess.Popen[bytes]],
) -> None:
    target = _session(leaders)
    bystander = _session(leaders)
    outcome = closure.close_sessions(
        [target],
        grace_s=0.2,
        kill_s=0.2,
        process_control=SYSTEM_PROCESS_CONTROL,
        own_pid=OWN_PID,
        timers=SYSTEM_TIMERS,
    )
    assert outcome == closure.Outcome(grace_s=0.2)
    assert bystander.shell.live()


def test_stale_birth_is_never_signalled(leaders: list[subprocess.Popen[bytes]]) -> None:
    target = _session(leaders)
    stale = OwnedProcess(
        target.shell.pid,
        target.shell.birth - 1,
        None if target.shell.starttime is None else target.shell.starttime - 1,
    )
    assert closure.close_sessions(
        [closure.Target(target.name, stale)],
        grace_s=0,
        kill_s=0,
        process_control=SYSTEM_PROCESS_CONTROL,
        own_pid=OWN_PID,
        timers=SYSTEM_TIMERS,
    ) == closure.Outcome(grace_s=0.0)
    assert target.shell.live()


def test_known_shell_survivor_is_reported_not_closed(
    leaders: list[subprocess.Popen[bytes]],
) -> None:
    target = _session(leaders)

    control = FakeProcessControl(groups={target.shell.pid: target.shell.pid, **OWN_GROUP})
    outcome = closure.close_sessions(
        [target],
        grace_s=0,
        kill_s=0,
        process_control=control,
        own_pid=OWN_PID,
        timers=SYSTEM_TIMERS,
    )
    assert outcome.closed == ()
    assert outcome.survivors == (closure.Survivor(target.name, target.shell, SurvivorRole.SHELL),)


def test_known_job_survivor_is_diagnostic_after_shell_closes(
    leaders: list[subprocess.Popen[bytes]],
) -> None:
    target, job = _session(leaders), _session(leaders)

    class ShellOnlyControl(FakeProcessControl):
        def killpg(self, pgid: int, signum: int, /) -> None:
            if pgid == target.shell.pid:
                SYSTEM_PROCESS_CONTROL.killpg(pgid, signum)

    control = ShellOnlyControl(forward_to=SYSTEM_PROCESS_CONTROL)
    outcome = closure.close_sessions(
        [closure.Target(target.name, target.shell, (job.shell,))],
        grace_s=0.2,
        kill_s=0.2,
        process_control=control,
        own_pid=OWN_PID,
        timers=SYSTEM_TIMERS,
    )
    assert outcome.survivors == (closure.Survivor(target.name, job.shell, SurvivorRole.JOB),)
    assert outcome.closed == (
        closure.ClosedSession(target.name, target.shell, ((job.shell.pid, "sleep"),)),
    )


def test_signal_permission_error_is_not_silent_success(
    leaders: list[subprocess.Popen[bytes]],
) -> None:
    target = _session(leaders)

    control = FakeProcessControl(groups={target.shell.pid: target.shell.pid, **OWN_GROUP})
    control.refuse(target.shell.pid, PermissionError("known group signal denied"))
    with pytest.raises(PermissionError, match="known group signal denied"):
        closure.close_sessions(
            [target],
            grace_s=0,
            kill_s=0,
            process_control=control,
            own_pid=OWN_PID,
            timers=SYSTEM_TIMERS,
        )
    assert target.shell.live()


def test_wire_outcome_round_trips_known_survivors() -> None:
    shell = OwnedProcess(4242, 12.5, 987654)
    job = OwnedProcess(4243, 12.75, None)
    outcome = closure.Outcome(
        0.5,
        closed=(closure.ClosedSession("meiva-agent-1-shell-1-wire", shell, ((4243, "sleep"),)),),
        survivors=(closure.Survivor("meiva-agent-1-shell-1-wire", job, SurvivorRole.JOB),),
        escalated=True,
    )
    assert closure.Outcome.from_wire(outcome.to_wire()) == outcome


def test_known_group_force_kill_covers_inheriting_child(
    leaders: list[subprocess.Popen[bytes]],
) -> None:
    import select
    import sys
    from pathlib import Path

    process = subprocess.Popen(
        [sys.executable, "base/sessions/pty/tests/closure_group_child.py"],
        cwd=Path(__file__).resolve().parents[4],
        start_new_session=True,
        stdout=subprocess.PIPE,
    )
    leaders.append(process)
    assert process.stdout is not None
    assert select.select([process.stdout], [], [], 10)[0], "job did not become ready"
    child_pid = int(process.stdout.readline())
    child = OwnedProcess.capture(psutil.Process(child_pid))
    shell = OwnedProcess.capture(psutil.Process(process.pid))
    control = FakeProcessControl(forward_to=SYSTEM_PROCESS_CONTROL)
    try:
        outcome = closure.close_sessions(
            [closure.Target("known-group", shell)],
            grace_s=0.05,
            kill_s=1,
            process_control=control,
            own_pid=OWN_PID,
            timers=SYSTEM_TIMERS,
        )
        assert outcome == closure.Outcome(grace_s=0.05, escalated=True)
        assert Signalled("killpg", shell.pid, signal.SIGKILL) in control.signals
        assert all(sent.signum != signal.SIGSTOP for sent in control.signals)
        assert process_groups.wait([child], 1, timers=SYSTEM_TIMERS) == ()
    finally:
        child.send_signal(signal.SIGKILL)
        process.stdout.close()


def test_known_process_in_callers_group_does_not_signal_whole_group() -> None:
    process = subprocess.Popen(["sleep", "300"])
    try:
        identity = OwnedProcess.capture(psutil.Process(process.pid))
        assert os.getpgid(process.pid) == os.getpgrp()
        process_groups.signal(
            [identity],
            signal.SIGKILL,
            process_control=SYSTEM_PROCESS_CONTROL,
            own_pid=OWN_PID,
            timers=SYSTEM_TIMERS,
        )
        assert process_groups.wait([identity], 1, timers=SYSTEM_TIMERS) == ()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


def test_wait_spends_its_budget_on_the_timers_it_is_given(
    leaders: list[subprocess.Popen[bytes]],
) -> None:
    target = _session(leaders)
    timers = ManualTimers()

    assert process_groups.wait([target.shell], 5.0, timers=timers) == (target.shell,)

    assert sum(timers.sleeps) == pytest.approx(5.0)
    assert timers.monotonic() == pytest.approx(5.0)


def test_signalling_the_process_named_as_the_callers_own_is_refused(
    leaders: list[subprocess.Popen[bytes]],
) -> None:
    named = _session(leaders)
    control = FakeProcessControl(groups={named.shell.pid: named.shell.pid})

    with pytest.raises(RuntimeError, match="cannot signal its own process"):
        process_groups.signal(
            [named.shell],
            signal.SIGTERM,
            process_control=control,
            own_pid=named.shell.pid,
            timers=SYSTEM_TIMERS,
        )

    assert control.signals == []
    assert named.shell.live()


def test_a_process_in_the_group_of_the_named_caller_is_signalled_alone(
    leaders: list[subprocess.Popen[bytes]],
) -> None:
    target = _session(leaders)
    named_caller = 4_000_000
    shared_group = 777
    control = FakeProcessControl(
        groups={target.shell.pid: shared_group, named_caller: shared_group}
    )

    process_groups.signal(
        [target.shell],
        signal.SIGKILL,
        process_control=control,
        own_pid=named_caller,
        timers=SYSTEM_TIMERS,
    )

    assert [sent for sent in control.signals if sent.kind == "killpg"] == []
    assert process_groups.wait([target.shell], 5, timers=SYSTEM_TIMERS) == ()
