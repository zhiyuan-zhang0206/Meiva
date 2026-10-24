# meiva:note
# What `meiva cluster destroy` asks, says and does, decided from values.
#
# Destroy is irreversible, so the order and the early exits are the invariants: the terminal
# intent is published before anything stops, the home is marked detached only after the stop
# and every retirement, and the data directories go last. The shell executing the plan against
# a real home and a real child is `test_cluster_destroy.py`.

from __future__ import annotations

from dataclasses import replace
from typing import Any

from cli.commands.cluster.plans.destroy_plan import (
    DestroyFacts,
    DestroyPlan,
    DropData,
    HostSystem,
    IntentState,
    PublishIntent,
    RetireHelper,
    RetireJobs,
    StopCluster,
    StopMode,
    answer_confirms,
    cleanup_incomplete,
    destroy_prompt,
    plan_destroy,
    scheduled_jobs_failure,
    stop_argv,
)

_COMMON = ("--yes", "--close-shell-sessions", "--stop-browser", "--stop-extras")


def _facts(**changes: Any) -> DestroyFacts:
    facts = DestroyFacts(
        home="/home/u/.meiva",
        roles=("agent-runner",),
        stop_mode=StopMode.SMOOTH,
        stop_timeout_s=300.0,
        meiva_executable="/src/.venv/bin/meiva",
        platform=HostSystem.LINUX,
        helper_port=8181,
        drop_dirs=(),
    )
    return replace(facts, **changes)


def test_destroy_stops_everything_with_the_operators_mode_and_budget() -> None:
    # meiva:note
    # A detach stops every component; only a gateway names its data plane.
    gateway = stop_argv("/src/.venv/bin/meiva", ["gateway"], StopMode.FORCE, 120.0)
    runner = stop_argv("/src/.venv/bin/meiva", ["agent-runner"], StopMode.SMOOTH, 60.0)

    assert gateway == (
        "/src/.venv/bin/meiva",
        "stop",
        *_COMMON,
        "--keep-no-services",
        "--mode",
        "force",
        "--timeout",
        "120",
        "--stop-infra",
    )
    assert runner == (
        "/src/.venv/bin/meiva",
        "stop",
        *_COMMON,
        "--keep-no-services",
        "--mode",
        "smooth",
        "--timeout",
        "60",
    )


def test_a_fractional_budget_keeps_its_fraction() -> None:
    assert stop_argv("m", ["agent-runner"], StopMode.SMOOTH, 7.5)[-1] == "7.5"


def test_the_terminal_intent_is_published_first_and_detached_only_after_everything_retired() -> (
    None
):
    plan = plan_destroy(_facts())

    assert plan == DestroyPlan(
        under_lock=(
            PublishIntent(IntentState.DESTROYING, announce=None),
            StopCluster(
                stop_argv("/src/.venv/bin/meiva", ["agent-runner"], StopMode.SMOOTH, 300.0)
            ),
            RetireJobs(),
            PublishIntent(
                IntentState.DETACHED, announce="detached /home/u/.meiva after exact cleanup"
            ),
        ),
        afterwards=(),
    )


def test_only_a_macos_host_has_a_permissions_helper_to_retire() -> None:
    darwin = plan_destroy(_facts(platform=HostSystem.DARWIN)).under_lock

    assert darwin[2:4] == (RetireJobs(), RetireHelper(8181))
    assert not any(isinstance(step, RetireHelper) for step in plan_destroy(_facts()).under_lock)


def test_the_data_directories_go_last_and_only_when_asked() -> None:
    dirs = ("/home/u/.meiva/pg", "/home/u/.meiva/redis", "/run/pg-sock")

    plan = plan_destroy(_facts(drop_dirs=dirs))

    assert plan.afterwards == (
        DropData(
            dirs,
            announce="✓ removed '/home/u/.meiva' per-cluster data plane (pg + redis data dirs)",
        ),
    )
    assert not any(isinstance(step, DropData) for step in plan.under_lock)
    assert plan_destroy(_facts()).afterwards == ()


def test_the_intent_marker_is_a_versioned_state() -> None:
    assert (
        PublishIntent(IntentState.DESTROYING, None).marker()
        == b'{"version":1,"state":"destroying"}\n'
    )
    assert (
        PublishIntent(IntentState.DETACHED, "x").marker() == b'{"version":1,"state":"detached"}\n'
    )


def test_the_prompt_says_what_happens_without_listing_a_deletion() -> None:
    lines = destroy_prompt(_facts())

    assert lines[0] == (
        "meiva cluster destroy will decommission the cluster at /home/u/.meiva (agent-runner):"
    )
    text = "\n".join(lines)
    assert "stop its services and its own Postgres and Redis" in text
    assert "mark the home detached" in text
    assert "DELETE" not in text and "cannot be undone" not in text
    assert "serves the gateway" not in text


def test_the_prompt_names_a_gateway_and_exactly_the_directories_that_go() -> None:
    dirs = ("/home/u/.meiva/pg", "/home/u/.meiva/redis", "/run/pg-sock")

    lines = destroy_prompt(_facts(roles=("agent-runner", "gateway"), drop_dirs=dirs))

    text = "\n".join(lines)
    assert "(agent-runner, gateway)" in text
    assert "this host serves the gateway: every agent-runner loses its control plane" in text
    assert "DELETE the cluster's database and cache, which cannot be undone:" in text
    assert [line.strip() for line in lines[-3:]] == list(dirs)


def test_a_home_with_no_recorded_roles_is_still_named() -> None:
    assert "(no roles recorded)" in destroy_prompt(_facts(roles=()))[0]


def test_only_the_exact_home_path_confirms() -> None:
    assert answer_confirms("/home/u/.meiva\n", "/home/u/.meiva")
    assert answer_confirms("  /home/u/.meiva  ", "/home/u/.meiva")
    for answer in ("", "y", "yes", "/somewhere/else", "home", "/home/u/.meiva/"):
        assert not answer_confirms(answer, "/home/u/.meiva")


def test_every_failed_job_is_named_with_its_reason() -> None:
    assert scheduled_jobs_failure([("autostart", "launchctl unavailable"), ("PR flow", "x")]) == (
        "could not remove scheduled jobs: autostart (launchctl unavailable), PR flow (x)"
    )


def test_an_incomplete_cleanup_says_so() -> None:
    assert (
        cleanup_incomplete("ownership unknown") == "cluster cleanup incomplete: ownership unknown"
    )
