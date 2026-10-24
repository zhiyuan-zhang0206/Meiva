# meiva:note
# What bringing the pooler up does for each state it can be found in: a plan over values, with
# no pooler and no clock.

from __future__ import annotations

import itertools

from cli.commands.data_plane.pooler.convergence import (
    AnnounceChangedRestart,
    AnnounceDegradedRestart,
    AwaitReachableBind,
    BindContext,
    Launch,
    PoolerObservation,
    Reload,
    StopRunning,
    WriteFiles,
    plan_convergence,
)

_FRESH_START = AwaitReachableBind(BindContext.FRESH_START)


# meiva:note
# A pooler that is not running is born only after the reachable address is assigned: one born
# earlier degrades to loopback-only while a loopback probe reads it as healthy.
def test_a_pooler_that_is_not_running_waits_for_the_address_and_starts() -> None:
    plan = plan_convergence(
        PoolerObservation(running=False, files_changed=True, public_listener=False)
    )

    assert plan == (WriteFiles(), _FRESH_START, Launch())


# meiva:note
# A running pooler whose files are unchanged and whose public listener verifies is reloaded and
# never waited on: a blip on the private network must not hold `meiva start` hostage behind a
# serving pooler.
def test_a_healthy_unchanged_pooler_is_reloaded_without_waiting() -> None:
    plan = plan_convergence(
        PoolerObservation(running=True, files_changed=False, public_listener=True)
    )

    assert plan == (WriteFiles(), Reload())


# meiva:note
# A changed userlist or ini restarts a healthy pooler: a reload keeps a removed user that
# already authenticated.
def test_changed_files_restart_a_healthy_pooler_never_reload_it() -> None:
    plan = plan_convergence(
        PoolerObservation(running=True, files_changed=True, public_listener=True)
    )

    assert plan == (WriteFiles(), AnnounceChangedRestart(), StopRunning(), _FRESH_START, Launch())


# meiva:note
# A pooler missing its public listener is restarted, because a reload does not retry a bind
# that failed at startup. The restart waits for the address before it stops the pooler it
# replaces, so a pooler that cannot be reborn healthy is left serving.
def test_a_degraded_pooler_is_restarted_after_the_address_is_there() -> None:
    plan = plan_convergence(
        PoolerObservation(running=True, files_changed=False, public_listener=False)
    )

    assert plan == (
        WriteFiles(),
        AwaitReachableBind(BindContext.RESTART_OF_DEGRADED),
        AnnounceDegradedRestart(),
        StopRunning(),
        _FRESH_START,
        Launch(),
    )


def test_a_degraded_pooler_restarts_whether_or_not_its_files_changed() -> None:
    unchanged = PoolerObservation(running=True, files_changed=False, public_listener=False)
    changed = PoolerObservation(running=True, files_changed=True, public_listener=False)

    assert plan_convergence(unchanged) == plan_convergence(changed)


# meiva:note
# Whatever the state, the files are written first and the plan ends in the step that proves the
# outcome: a reload or a launch.
def test_every_plan_writes_first_and_ends_in_a_reload_or_a_launch() -> None:
    for running, changed, public in itertools.product((False, True), repeat=3):
        plan = plan_convergence(PoolerObservation(running, changed, public))

        assert plan[0] == WriteFiles()
        assert plan[-1] in (Reload(), Launch())
        assert (StopRunning() in plan) == (running and not (public and not changed))
