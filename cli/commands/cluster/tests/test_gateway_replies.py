# meiva:note
# What the machine verbs of `meiva cluster` print for each gateway reply, decided from values.

from __future__ import annotations

from cli.commands.cluster.plans.gateway_replies import (
    NoGatewayUrl,
    PauseReply,
    ReadTimedOut,
    RefusedWithStatus,
    Unreachable,
    not_registered_line,
    pause_lines,
    resume_lines,
    roster_failure_line,
    staging_line,
)


def test_an_unknown_machine_is_named_with_its_quoted_name() -> None:
    assert not_registered_line("runner-a") == "no machine named 'runner-a' in the registry"


def test_marking_and_unmarking_staging_say_what_the_machine_now_is() -> None:
    assert staging_line("runner-a", is_staging=True) == "runner-a: marked staging"
    assert (
        staging_line("runner-a", is_staging=False)
        == "runner-a: unmarked staging (now a fan-out target)"
    )


def test_a_pause_names_what_the_machine_is_hidden_from_and_no_rollout() -> None:
    lines = pause_lines(
        "runner-a",
        PauseReply(
            paused_at="2026-09-27T00:00:00+00:00",
            pause_reason="",
            terminated_agents=2,
            force_marked_agents=1,
            reassigned_tasks=5,
        ),
    )

    assert lines[0] == "runner-a: PAUSED (since 2026-09-27T00:00:00)"
    assert lines[1] == (
        "  agents terminated via the machine's ops server: 2; "
        "force-marked in DB (machine unreachable): 1; tasks drained to #405: 5"
    )
    assert "hidden from roster/probe/fan-out/spawn until resumed" in lines[2]
    assert "`meiva cluster resume runner-a`" in lines[2]
    assert "rollout" not in "\n".join(lines)


def test_a_pause_reason_follows_the_verdict() -> None:
    lines = pause_lines("runner-a", PauseReply("2026-09-27T00:00:00+00:00", "disk swap", 0, 0, 0))

    assert lines[0].endswith("(since 2026-09-27T00:00:00) — disk swap")


def test_a_resume_names_what_is_restored_and_the_machine_side_checklist() -> None:
    verdict, checklist = resume_lines("runner-a", resumed=True)

    assert verdict == "runner-a: resumed — probing / roster / fan-out / spawn restored"
    assert "run ON runner-a when it is back online" in checklist
    assert "`meiva restart --all-services" in checklist
    assert "rollout" not in verdict


def test_resuming_a_machine_that_was_not_paused_says_so() -> None:
    verdict, _checklist = resume_lines("runner-a", resumed=False)

    assert verdict == "runner-a: was not paused (no-op)"


def test_each_way_the_roster_read_fails_gets_its_own_line() -> None:
    assert roster_failure_line(NoGatewayUrl("MEIVA_GATEWAY_URL unset")) == (
        "✗ cannot resolve gateway URL: MEIVA_GATEWAY_URL unset"
    )
    assert roster_failure_line(Unreachable("http://gw:8000/x", "connection refused")) == (
        "✗ gateway unreachable at http://gw:8000/x: connection refused"
    )
    assert roster_failure_line(RefusedWithStatus("http://gw:8000/x", 503, "boom")) == (
        "✗ gateway returned HTTP 503 for http://gw:8000/x: boom"
    )


def test_a_read_timeout_names_the_budget_and_blames_the_gateway_itself() -> None:
    line = roster_failure_line(ReadTimedOut("http://gw:8000/x", 9.0, 4.0, "timed out"))

    assert line.startswith("✗ gateway at http://gw:8000/x did not respond within 9s")
    assert "adds 4s of slack on top" in line
    assert line.endswith("gateway itself not answering: timed out")
