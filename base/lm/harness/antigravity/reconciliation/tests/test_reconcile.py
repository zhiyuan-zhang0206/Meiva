from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path

import pytest

from base.lm.harness.antigravity.reconciliation.judge import QueuedMessage, judge
from base.lm.harness.antigravity.reconciliation.payload import client_message_id
from base.lm.harness.antigravity.reconciliation.queue_files import queue_dir, reconcile_inputs
from base.lm.harness.antigravity.trajectory import AgyInterfaceError
from base.lm.harness.delivery import Reconciliation

FIXTURE = Path(__file__).parent / "fixtures" / "conversation-1" / "messages"


# meiva:note
# The fixture is a minimal re-encoding of a recorded queue file (agy 1.5.5): the same two-level field
# layout (step field 19 = user input, whose field 19 = clientMessageId), without the recorded paths.


def reconcile_input(home: Path, session: str, key: str) -> Reconciliation | None:
    answers = reconcile_inputs(home, session, [key])
    return None if answers is None else answers[key]


def home_with_queue(tmp_path: Path) -> Path:
    target = queue_dir(tmp_path, "conversation-1")
    target.parent.mkdir(parents=True)
    _ = shutil.copytree(FIXTURE, target)
    return tmp_path


def test_a_delivered_message_is_in_context_and_a_stored_one_is_not_sent_again() -> None:
    queue = [
        QueuedMessage("k-delivered", delivered=True, undelivered=False),
        QueuedMessage("k-waiting", delivered=False, undelivered=True),
    ]
    assert judge(queue, "k-delivered") is Reconciliation.IN_CONTEXT
    assert judge(queue, "k-waiting") is Reconciliation.PERSISTED_UNDELIVERED
    assert judge(queue, "k-never") is Reconciliation.UNKNOWN, "a stdin input leaves no file"


def test_the_queue_is_read_from_the_injected_home(tmp_path: Path) -> None:
    home = home_with_queue(tmp_path)
    assert reconcile_input(home, "conversation-1", "k-delivered") is Reconciliation.IN_CONTEXT
    assert (
        reconcile_input(home, "conversation-1", "k-waiting") is Reconciliation.PERSISTED_UNDELIVERED
    )
    assert reconcile_input(home, "conversation-1", "k-lost") is Reconciliation.UNKNOWN
    assert reconcile_input(home, "conversation-2", "k-lost") is None


def test_an_unreadable_queue_file_is_an_error_not_a_missing_input(tmp_path: Path) -> None:
    home = home_with_queue(tmp_path)
    broken = queue_dir(home, "conversation-1") / "33333333-cccc-4ccc-8ccc-333333333333.json"
    broken.write_text('{"stepPayload": "***"}')
    with pytest.raises(AgyInterfaceError, match="base64"):
        _ = reconcile_input(home, "conversation-1", "k-delivered")


def test_a_truncated_protobuf_is_refused() -> None:
    truncated = base64.b64encode(bytes([0x9A, 0x01, 0x40, 0x01])).decode()
    with pytest.raises(AgyInterfaceError, match="truncated"):
        _ = client_message_id(truncated)
    assert client_message_id(base64.b64encode(bytes([0x08, 0x0E])).decode()) is None


def test_internal_hub_notices_do_not_affect_input_reconciliation(tmp_path: Path) -> None:
    home = home_with_queue(tmp_path)
    directory = queue_dir(home, "conversation-1")
    for index, sender in enumerate(["system", "conversation-1/task-7301", "subagent-conversation"]):
        notice = directory / f"33333333-cccc-4ccc-8ccc-{index:012d}.json"
        _ = notice.write_text(json.dumps({"sender": sender, "content": "Internal notice"}))
    assert reconcile_inputs(home, "conversation-1", ["k-delivered", "k-waiting", "k-lost"]) == {
        "k-delivered": Reconciliation.IN_CONTEXT,
        "k-waiting": Reconciliation.PERSISTED_UNDELIVERED,
        "k-lost": Reconciliation.UNKNOWN,
    }


@pytest.mark.parametrize(
    "entry",
    [
        {"sender": "user", "content": "hi"},
        {},
        {"stepPayload": None},
        {"sender": "system", "content": 123},
        {"sender": "", "content": "hi"},
        {"content": "hi"},
        {"sender": 123, "content": "hi"},
        {"sender": "system"},
        {"sender": "system", "content": "hi", "stepPayload": None},
        [],
    ],
)
def test_malformed_queue_entries_are_refused(tmp_path: Path, entry: object) -> None:
    home = home_with_queue(tmp_path)
    broken = queue_dir(home, "conversation-1") / "33333333-cccc-4ccc-8ccc-333333333333.json"
    _ = broken.write_text(json.dumps(entry))
    with pytest.raises(AgyInterfaceError, match="a queued message has no stepPayload"):
        _ = reconcile_inputs(home, "conversation-1", ["k-delivered"])
