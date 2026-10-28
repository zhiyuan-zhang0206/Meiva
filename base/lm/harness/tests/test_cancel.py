from __future__ import annotations

from base.lm.harness.cancel import (
    CancelReport,
    ProcessObservation,
    Sweep,
    describe,
    observed_cancel,
    spawned_since,
    term_candidates,
    unobserved_cancel,
)

TREE = {10: "claude --print", 11: "bash -c sleep 90", 12: "sleep 90", 13: "node server.js"}


def test_describe_names_each_pid_with_its_command_in_pid_order() -> None:
    assert describe(TREE, [12, 10]) == (
        ProcessObservation(pid=10, command="claude --print"),
        ProcessObservation(pid=12, command="sleep 90"),
    )
    assert describe(TREE, [99]) == (ProcessObservation(pid=99, command=""),)


def test_spawned_since_keeps_what_was_not_there_at_the_baseline() -> None:
    assert spawned_since(TREE, {10: "claude --print", 13: "node server.js"}) == {
        11: "bash -c sleep 90",
        12: "sleep 90",
    }
    assert spawned_since(TREE, TREE) == {}
    assert spawned_since(TREE, {}) == TREE


def test_an_unobserved_cancel_claims_nothing_about_the_tree() -> None:
    assert unobserved_cancel(turn_id="turn-1", interrupt_ms=3.5) == CancelReport(
        turn_id="turn-1",
        interrupt_ms=3.5,
        settled=None,
        observed=(),
        remaining=(),
        waited_ms=0.0,
        cli_pid=None,
        cli_alive=None,
    )


def test_an_unobserved_cancel_can_say_what_is_known_of_the_cli() -> None:
    report = unobserved_cancel(turn_id=None, cli_pid=4242, cli_alive=True)

    assert (report.cli_pid, report.cli_alive, report.settled) == (4242, True, None)
    assert report.interrupt_ms == 0.0


def test_a_clean_sweep_settles_and_leaves_nothing_remaining() -> None:
    report = observed_cancel(
        turn_id="turn-2",
        interrupt_ms=12.0,
        pid=10,
        cli_alive=True,
        sweep=Sweep(
            observed=TREE,
            spawned={11: TREE[11], 12: TREE[12]},
            gone=True,
            waited_ms=40.0,
            alive=(),
        ),
    )

    assert report.settled is True
    assert report.observed == describe(TREE, [11, 12])
    assert report.remaining == ()
    assert (report.cli_pid, report.cli_alive, report.waited_ms) == (10, True, 40.0)
    assert report.killed == ()


def test_a_process_that_outlived_the_sweep_is_named_and_the_sweep_is_not_settled() -> None:
    report = observed_cancel(
        turn_id="turn-3",
        interrupt_ms=12.0,
        pid=10,
        cli_alive=False,
        sweep=Sweep(
            observed=TREE,
            spawned={11: TREE[11], 12: TREE[12]},
            gone=False,
            waited_ms=2000.0,
            alive=[12],
        ),
        killed=(11,),
    )

    assert report.settled is False
    assert report.remaining == describe(TREE, [12])
    assert report.observed == describe(TREE, [11, 12])
    assert report.killed == (11,)
    assert report.cli_alive is False


def test_term_candidates_keeps_the_live_descendants_in_pid_order() -> None:
    assert term_candidates({12: "sleep 90", 10: "bash -c sleep 90"}, agent_pid=10, alive=[12]) == (
        12,
    )
    assert term_candidates(TREE, agent_pid=10, alive=[13, 11]) == (11, 13)


def test_term_candidates_never_signals_the_runtime_itself() -> None:
    assert term_candidates({10: "dsh", 11: "sleep 90"}, agent_pid=10, alive=[10, 11]) == (11,)
    assert term_candidates({11: "sleep 90"}, agent_pid=None, alive=[11]) == (11,)


def test_term_candidates_skips_what_the_probe_no_longer_sees() -> None:
    assert term_candidates(TREE, agent_pid=10, alive=[]) == ()
    assert term_candidates(TREE, agent_pid=None, alive=[99]) == ()
