from __future__ import annotations

from services.agent_runner.agent_host.invocation.failure_shapes import (
    cleanup_failure,
    sole_invocation_error,
)


def test_a_group_that_wrapped_only_the_invocations_own_error_gives_it_back() -> None:
    error = RuntimeError("the invocation failed")

    assert sole_invocation_error(BaseExceptionGroup("tasks", [error]), error) is error


def test_a_worker_failure_beside_the_invocation_error_keeps_the_group() -> None:
    error, worker = RuntimeError("the invocation failed"), ValueError("a worker failed")

    assert sole_invocation_error(BaseExceptionGroup("tasks", [error, worker]), error) is None


def test_a_group_of_some_other_single_error_is_not_the_invocations() -> None:
    error, other = RuntimeError("the invocation failed"), RuntimeError("the invocation failed")

    assert sole_invocation_error(BaseExceptionGroup("tasks", [other]), error) is None


def test_without_an_invocation_error_the_group_stays_a_group() -> None:
    assert (
        sole_invocation_error(BaseExceptionGroup("tasks", [ValueError("a worker")]), None) is None
    )


def test_a_failed_cleanup_after_a_failed_invocation_leaves_both_in_order() -> None:
    invocation, cleanup = RuntimeError("the invocation failed"), OSError("the publisher broke")

    group = cleanup_failure(invocation, cleanup)

    assert group.exceptions == (invocation, cleanup)
    assert str(group).startswith("invocation and publisher cleanup failed")
