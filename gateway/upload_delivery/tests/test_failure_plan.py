# meiva:note
# When a failed copy of a delivered upload is retried and when it is held: values in, a verdict out.

import pytest

from gateway.upload_delivery.failure_plan import CopyFailure, receiver_failure, terminal_failure


def test_a_runner_without_the_receive_op_holds_the_batch() -> None:
    result = {"error": "unknown kind: 'upload-receive-v2'"}
    assert receiver_failure(result) == CopyFailure("unsupported-receiver", hold=True)


@pytest.mark.parametrize(
    "reason", ["upload-copy-conflict-v1", "upload-copy-quota-v1", "upload-source-unavailable-v1"]
)
def test_a_refusal_that_cannot_change_holds_the_batch_under_its_own_reason(reason: str) -> None:
    result = {"error": "UploadDeliveryConflictError: x", "reason": reason}
    assert receiver_failure(result) == CopyFailure(reason, hold=True)


@pytest.mark.parametrize(
    "result",
    [{"error": "OSError: disk"}, {"error": "x", "reason": "something-new"}, {}],
)
def test_any_other_receiver_failure_is_retried(result: dict[str, object]) -> None:
    assert receiver_failure(result) == CopyFailure("receiver-unavailable", hold=False)


def test_a_conflict_names_itself_and_other_terminal_errors_name_their_type() -> None:
    assert terminal_failure(
        "UploadDeliveryConflictError", "stored intent differs", conflict=True
    ) == (CopyFailure("stored intent differs", hold=True))
    assert terminal_failure("AgentNotFound", "agent 9", conflict=False) == CopyFailure(
        "AgentNotFound", hold=True
    )
