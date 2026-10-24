# meiva:note
# How a keyed operation is claimed, replayed and retried, over values.

from __future__ import annotations

import pytest

from ops.rpc_schemas import OpStatus
from services.agent_runner.agent_ops.idempotency_plan import (
    Conflict,
    Replay,
    Wait,
    conflict_failure,
    request_hash,
    retry_pass,
    stored_body,
    stored_verdict,
    uncertain_failure,
)

DIGEST = request_hash("status_probe", {"a": 1, "b": 2})


def test_the_same_request_in_another_key_order_has_the_same_identity() -> None:
    assert request_hash("status_probe", {"a": 1, "b": 2}) == request_hash(
        "status_probe", {"b": 2, "a": 1}
    )


def test_a_changed_kind_or_payload_changes_the_identity() -> None:
    assert (
        len(
            {
                DIGEST,
                request_hash("lifecycle", {"a": 1, "b": 2}),
                request_hash("status_probe", {"a": 1}),
            }
        )
        == 3
    )


def test_a_missing_row_is_waited_on() -> None:
    assert stored_verdict(None, "status_probe", DIGEST) == Wait()


def test_a_row_without_a_status_belongs_to_a_running_or_dead_owner() -> None:
    row: tuple[object, ...] = ("ops", "status_probe", DIGEST, None, {})
    assert stored_verdict(row, "status_probe", DIGEST) == Wait()


@pytest.mark.parametrize("status", list(OpStatus))
def test_a_stored_status_is_replayed_with_its_result(status: OpStatus) -> None:
    row: tuple[object, ...] = ("ops", "status_probe", DIGEST, status.value, {"k": "v"})
    assert stored_verdict(row, "status_probe", DIGEST) == Replay(status, {"k": "v"})


def test_a_stored_result_that_is_empty_replays_as_an_empty_result() -> None:
    row: tuple[object, ...] = ("ops", "status_probe", DIGEST, "completed", None)
    assert stored_verdict(row, "status_probe", DIGEST) == Replay(OpStatus.COMPLETED, {})


@pytest.mark.parametrize("status", ["pending", "", "enqueued"])
def test_a_stored_status_outside_the_vocabulary_raises(status: str) -> None:
    row: tuple[object, ...] = ("ops", "status_probe", DIGEST, status, {})
    with pytest.raises(ValueError, match="OpStatus"):
        stored_verdict(row, "status_probe", DIGEST)


@pytest.mark.parametrize(
    "row",
    [
        ("api", "status_probe", DIGEST, "completed", {}),
        ("ops", "lifecycle", DIGEST, "completed", {}),
        ("ops", "status_probe", "another-identity", "completed", {}),
        ("ops", "status_probe", None, "completed", {}),
    ],
)
def test_a_row_of_another_request_or_without_an_identity_is_a_conflict(
    row: tuple[object, ...],
) -> None:
    assert stored_verdict(row, "status_probe", DIGEST) == Conflict()


def test_the_refusals_are_failed_results_that_say_the_operation_was_not_re_executed() -> None:
    status, conflict = conflict_failure()
    assert status is OpStatus.FAILED
    assert "identity conflict" in str(conflict["error"])
    assert "not re-executed" in str(conflict["error"])
    status, uncertain = uncertain_failure("key-1")
    assert status is OpStatus.FAILED
    assert "'key-1'" in str(uncertain["error"])
    assert "outcome uncertain" in str(uncertain["error"])


def test_a_payload_and_a_result_are_stored_as_json_text() -> None:
    assert stored_body({"a": 1}) == '{"a": 1}'
    assert stored_body({"at": object}).startswith('{"at": "<class')


@pytest.mark.parametrize(("failed", "retried"), [(1, True), (2, True), (3, False), (4, False)])
def test_a_lost_connection_is_retried_only_while_attempts_are_left(
    failed: int, retried: bool
) -> None:
    assert retry_pass(failed, 3) is retried
