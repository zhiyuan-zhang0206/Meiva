from pathlib import Path

import pytest

from agent.graph.exec._child_boundary import ParentFacts, plan_child_launch
from agent.graph.exec._dispatch import (
    SYSTEM_HALT_NOTE,
    DispatchKind,
    crashed_no_output_body,
    plan_dispatch,
)
from agent.graph.exec._poll import KEEPALIVE_INTERVAL_S, Stop, plan_poll
from agent.graph.exec._result import (
    ExecCancelled,
    ExecChildError,
    ExecCrashed,
    ExecDone,
    ExecLifecycle,
    ExecTimedOut,
    LifecycleKind,
)
from base.agents.messages.inbound import InterruptReason
from base.agents.messages.kwargs import ExecStatus
from base.host.env.registry import AGENT_BIRTH_CONFIG_ENV, AGENT_CONFIG_OVERLAY_ENV


def test_a_finished_run_continues_the_turn() -> None:
    plan = plan_dispatch(ExecDone(output="hello\n"))

    assert (plan.kind, plan.halted, plan.status) == (
        DispatchKind.DONE,
        False,
        ExecStatus.COMPLETED,
    )
    assert plan.body == "hello\n"
    assert plan.writes_back


def test_a_timeout_is_feedback_not_a_halt() -> None:
    plan = plan_dispatch(ExecTimedOut(output="part\n"))

    assert (plan.kind, plan.halted, plan.status) == (
        DispatchKind.TIMED_OUT,
        False,
        ExecStatus.TIMED_OUT,
    )
    assert plan.timed_out and not plan.cancelled


@pytest.mark.parametrize("reason", list(InterruptReason))
def test_a_cancel_halts_and_names_what_ended_the_run(reason: InterruptReason) -> None:
    plan = plan_dispatch(ExecCancelled(output="part\n", reason=reason))

    assert (plan.kind, plan.halted, plan.status) == (
        DispatchKind.CANCELLED,
        True,
        ExecStatus.CANCELLED,
    )
    assert plan.cancelled and not plan.timed_out
    assert plan.cancel_reason is reason


@pytest.mark.parametrize("kind", [LifecycleKind.TERMINATION, LifecycleKind.RESTART])
def test_terminate_and_restart_halt_without_a_note(kind: LifecycleKind) -> None:
    plan = plan_dispatch(ExecLifecycle(output="before\n", kind=kind))

    assert (plan.kind, plan.halted, plan.status) == (
        DispatchKind.LIFECYCLE,
        True,
        ExecStatus.HALTED,
    )
    assert plan.body == "before\n"
    assert plan.halt_log == f"lifecycle {kind.value}"
    assert plan.writes_back


@pytest.mark.parametrize(("output", "joined"), [("", ""), ("work\n", "work\n"), ("work", "work\n")])
def test_compaction_appends_its_note_and_writes_nothing_back(output: str, joined: str) -> None:
    plan = plan_dispatch(ExecLifecycle(output=output, kind=LifecycleKind.SYSTEM_HALT))

    assert (plan.kind, plan.halted, plan.status) == (
        DispatchKind.COMPACT,
        True,
        ExecStatus.HALTED,
    )
    assert plan.body == joined + SYSTEM_HALT_NOTE
    assert not plan.writes_back


def test_a_crash_with_output_keeps_the_agent_facing_traceback() -> None:
    result = ExecCrashed(
        output="Traceback (most recent call last):\n  boom\n",
        exc=ExecChildError("ValueError", "boom", None),
        code_reached=True,
    )

    plan = plan_dispatch(result)

    assert (plan.kind, plan.halted, plan.status) == (
        DispatchKind.CRASHED,
        False,
        ExecStatus.FAILED,
    )
    assert plan.body == result.output
    assert not plan.boot_failed


@pytest.mark.parametrize(
    ("code_reached", "verdict", "boot_failed"),
    [
        (False, "the code was NOT executed", True),
        (None, "whether the code executed is unknown", False),
        (True, "your code may have had effects, but no output was recovered", False),
    ],
)
def test_a_crash_without_output_says_what_is_known_about_the_code(
    code_reached: bool | None, verdict: str, boot_failed: bool
) -> None:
    exc = ExecChildError("BootstrapFetchError", "could not fetch cluster config", "tb")

    plan = plan_dispatch(
        ExecCrashed(output="", exc=exc, full_traceback="tb", code_reached=code_reached)
    )

    assert verdict in plan.body
    assert "BootstrapFetchError: could not fetch cluster config" in plan.body
    assert "(no output)" not in plan.body
    assert plan.boot_failed is boot_failed
    assert plan.crash is not None and plan.crash.child_traceback == "tb"


def test_the_crash_names_come_from_the_child_envelope_or_the_exception_type() -> None:
    from_child = plan_dispatch(ExecCrashed(output="o", exc=ExecChildError("X", "y", None)))
    parent_side = plan_dispatch(ExecCrashed(output="o", exc=ValueError("plain")))

    assert from_child.crash is not None
    assert (from_child.crash.exc_type, from_child.crash.exc_msg) == ("X", "y")
    assert parent_side.crash is not None
    assert (parent_side.crash.exc_type, parent_side.crash.exc_msg) == ("ValueError", "plain")


def test_the_empty_crash_body_forms() -> None:
    plan = plan_dispatch(ExecCrashed(output="", exc=ExecChildError("X", "y", None)))
    assert plan.crash is not None

    assert "NOT executed" in crashed_no_output_body(plan.crash, code_reached=False)
    assert "X: y" in crashed_no_output_body(plan.crash, code_reached=False)
    assert "unknown" in crashed_no_output_body(plan.crash, code_reached=None)


def _beat(
    *,
    pending: bool = False,
    now: float = 0.0,
    last_activity: float = 0.0,
    deadline: float = 100.0,
    cancel_requested: bool = False,
):
    return plan_poll(
        pending=pending,
        now=now,
        last_activity=last_activity,
        deadline=deadline,
        cancel_requested=cancel_requested,
    )


def test_a_quiet_beat_inside_the_deadline_only_waits() -> None:
    plan = _beat(now=0.1)

    assert (plan.forward, plan.keepalive, plan.stop) == (False, False, None)


def test_accumulated_text_is_forwarded_and_needs_no_keepalive() -> None:
    plan = _beat(pending=True, now=10.0, last_activity=0.0)

    assert (plan.forward, plan.keepalive) == (True, False)


def test_a_silent_exec_is_kept_alive_from_the_keepalive_interval_on() -> None:
    assert not _beat(now=KEEPALIVE_INTERVAL_S - 0.01).keepalive
    assert _beat(now=KEEPALIVE_INTERVAL_S).keepalive


def test_real_output_restarts_the_keepalive_interval() -> None:
    last_activity = 0.75

    assert not _beat(now=1.24, last_activity=last_activity).keepalive
    assert _beat(now=1.25, last_activity=last_activity).keepalive


def test_the_deadline_stops_the_run_only_once_passed() -> None:
    assert _beat(now=100.0, deadline=100.0).stop is None
    assert _beat(now=100.01, deadline=100.0).stop is Stop.DEADLINE


def test_a_cancel_stops_the_run() -> None:
    assert _beat(cancel_requested=True).stop is Stop.CANCEL


@pytest.mark.parametrize("pending", [False, True])
def test_cancel_outranks_the_deadline_on_the_same_beat(pending: bool) -> None:
    plan = _beat(pending=pending, now=200.0, deadline=100.0, cancel_requested=True)

    assert plan.stop is Stop.CANCEL
    assert plan.forward is pending


def _parent(
    *, cwd: Path, source_root: Path | None, environ: dict[str, str] | None = None
) -> ParentFacts:
    return ParentFacts(
        environ={"VIRTUAL_ENV": "/source/.venv", "PATH": "/bin"} if environ is None else environ,
        cwd=cwd,
        executable="/source/.venv/bin/python",
        source_root=source_root,
        editable_roots=(),
    )


def _plan(parent: ParentFacts, tmp_path: Path, **overlays: dict[str, object]):
    return plan_child_launch(
        parent,
        None,
        tmp_path / "request",
        tmp_path / "result",
        config_overlay=overlays.get("config_overlay"),
        birth_config=overlays.get("birth_config"),
    )


def test_the_child_is_a_fresh_isolated_interpreter_in_the_parents_directory(
    tmp_path: Path,
) -> None:
    launch = _plan(_parent(cwd=tmp_path, source_root=None), tmp_path)

    assert launch.argv == (
        "/source/.venv/bin/python",
        "-I",
        "-B",
        "-X",
        "utf8",
        "-m",
        "agent.execution.child",
    )
    assert launch.cwd == tmp_path


def test_the_child_environment_names_the_envelopes_and_the_agent(tmp_path: Path) -> None:
    parent = _parent(cwd=tmp_path, source_root=None)

    launch = plan_child_launch(
        parent,
        424242,
        tmp_path / "request",
        tmp_path / "result",
        config_overlay=None,
        birth_config=None,
    )

    assert launch.env["MEIVA_AGENT_ID"] == "424242"
    assert launch.env["MEIVA_PROCESS_PROFILE"] == "agent"
    assert launch.env["MEIVA_EXEC_REQUEST_FILE"] == str(tmp_path / "request")
    assert launch.env["MEIVA_EXEC_RESULT_FILE"] == str(tmp_path / "result")
    assert launch.env["PATH"] == "/bin"
    assert parent.environ == {"VIRTUAL_ENV": "/source/.venv", "PATH": "/bin"}


def test_only_this_agents_explicit_config_maps_ride_the_environment(tmp_path: Path) -> None:
    ambient = {
        AGENT_CONFIG_OVERLAY_ENV: '{"llm_model": "another-agents-pin"}',
        AGENT_BIRTH_CONFIG_ENV: '{"llm_model": "another-agents-birth"}',
    }
    parent = _parent(cwd=tmp_path, source_root=None, environ=ambient)

    unbound = _plan(parent, tmp_path)
    bound = _plan(
        parent,
        tmp_path,
        config_overlay={"llm_model": "pinned", "a": 1},
        birth_config={"b": 2},
    )

    assert AGENT_CONFIG_OVERLAY_ENV not in unbound.env
    assert AGENT_BIRTH_CONFIG_ENV not in unbound.env
    assert bound.env[AGENT_CONFIG_OVERLAY_ENV] == '{"a": 1, "llm_model": "pinned"}'
    assert bound.env[AGENT_BIRTH_CONFIG_ENV] == '{"b": 2}'


@pytest.mark.parametrize(
    ("cwd", "keeps_virtual_env"),
    [
        ("source/agent", True),
        ("source", True),
        ("outside", False),
        ("source/.worktrees/feature", False),
        ("source/.claude/worktrees/feature", False),
    ],
)
def test_a_child_keeps_the_virtual_env_only_inside_its_own_checkout(
    tmp_path: Path, cwd: str, keeps_virtual_env: bool
) -> None:
    parent = _parent(cwd=tmp_path / cwd, source_root=tmp_path / "source")

    launch = _plan(parent, tmp_path)

    assert ("VIRTUAL_ENV" in launch.env) is keeps_virtual_env


def test_an_interpreter_outside_a_checkout_keeps_the_virtual_env_anywhere(
    tmp_path: Path,
) -> None:
    launch = _plan(_parent(cwd=tmp_path / "anywhere", source_root=None), tmp_path)

    assert launch.env["VIRTUAL_ENV"] == "/source/.venv"
