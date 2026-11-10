from __future__ import annotations

import base64
import shutil
from pathlib import Path

import pytest

from base.lm.harness.antigravity.reconciliation.judge import QueuedMessage, judge
from base.lm.harness.antigravity.reconciliation.payload import client_message_id
from base.lm.harness.antigravity.reconciliation.queue_files import queue_dir, reconcile_input
from base.lm.harness.antigravity.trajectory import AgyInterfaceError
from base.lm.harness.delivery import Reconciliation

FIXTURE = Path(__file__).parent / "fixtures" / "conversation-1" / "messages"


# meiva:note
# The fixture is a minimal re-encoding of a recorded queue file (agy 1.5.5): the same two-level field
# layout (step field 19 = user input, whose field 19 = clientMessageId), without the recorded paths.
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
    assert judge(queue, "k-never") is Reconciliation.NOT_FOUND


def test_the_queue_is_read_from_the_injected_home(tmp_path: Path) -> None:
    home = home_with_queue(tmp_path)
    assert reconcile_input(home, "conversation-1", "k-delivered") is Reconciliation.IN_CONTEXT
    assert (
        reconcile_input(home, "conversation-1", "k-waiting") is Reconciliation.PERSISTED_UNDELIVERED
    )
    assert reconcile_input(home, "conversation-1", "k-lost") is Reconciliation.NOT_FOUND
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
