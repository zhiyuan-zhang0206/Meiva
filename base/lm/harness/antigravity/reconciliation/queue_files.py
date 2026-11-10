from __future__ import annotations

import json
from pathlib import Path

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.lm.harness.antigravity.reconciliation.judge import QueuedMessage, judge
from base.lm.harness.antigravity.reconciliation.payload import client_message_id
from base.lm.harness.antigravity.trajectory import AgyInterfaceError
from base.lm.harness.delivery import Reconciliation


# meiva:note
# The read half of Antigravity reconciliation. The queue lives under the injected home at
# `.gemini/antigravity-cli/brain/<conversation>/.system_generated/messages/`: one `<uuid>.json` per
# message (`id`, `recipient`, `sender`, `timestamp`, `stepPayload`), `read.json` mapping the ids that
# were delivered, and `undelivered/` holding an entry per message still waiting. The layout was
# observed on agy 1.5.5 and is not a published interface; a file that is not the expected shape is an
# `AgyInterfaceError`, not a skipped message. A conversation with no queue directory answers None.
# meiva:link base/lm/harness/antigravity/docs/antigravity.md
def queue_dir(home: Path, conversation_id: str) -> Path:
    return (
        home
        / ".gemini"
        / "antigravity-cli"
        / "brain"
        / conversation_id
        / ".system_generated"
        / "messages"
    )


def read_queue(home: Path, conversation_id: str) -> list[QueuedMessage] | None:
    directory = queue_dir(home, conversation_id)
    if not directory.is_dir():
        return None
    delivered = _read_ids(directory / "read.json")
    waiting = _waiting_ids(directory / "undelivered")
    messages: list[QueuedMessage] = []
    for path in sorted(directory.glob("*-*.json")):
        parsed = object_of(json.loads, path.read_text())
        payload = parsed.get("stepPayload") if is_object_dict(parsed) else None
        if not isinstance(payload, str):
            raise AgyInterfaceError(f"{path}: a queued message has no stepPayload")
        message_id = path.stem
        messages.append(
            QueuedMessage(
                client_message_id=client_message_id(payload),
                delivered=message_id in delivered,
                undelivered=message_id in waiting,
            )
        )
    return messages


def reconcile_input(home: Path, conversation_id: str, key: str) -> Reconciliation | None:
    messages = read_queue(home, conversation_id)
    return None if messages is None else judge(messages, key)


def _read_ids(path: Path) -> frozenset[str]:
    if not path.is_file():
        return frozenset()
    parsed = object_of(json.loads, path.read_text())
    if not is_object_dict(parsed):
        raise AgyInterfaceError(f"{path}: read.json is not an object")
    return frozenset(parsed)


def _waiting_ids(directory: Path) -> frozenset[str]:
    if not directory.is_dir():
        return frozenset()
    return frozenset(entry.name.removesuffix(".json") for entry in directory.iterdir())
