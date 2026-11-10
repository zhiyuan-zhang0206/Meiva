from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from base.lm.harness.delivery import Reconciliation


# meiva:note
# One message file of the CLI's durable queue, already read: the id the file was written with (the
# `clientMessageId` from its payload, None when the payload carries none), whether `read.json` lists
# it (delivered to the model), and whether `undelivered/` lists it (stored, to be delivered by the CLI
# itself before the next stdin input).
@dataclass(frozen=True, slots=True)
class QueuedMessage:
    client_message_id: str | None
    delivered: bool
    undelivered: bool


# meiva:note
# Decide from the queue whether one input reached the model. A message in `read.json` was delivered:
# IN_CONTEXT. A message that is stored but not delivered, whether or not `undelivered/` lists it,
# will be delivered by the CLI itself, so it is PERSISTED_UNDELIVERED and must not be sent again.
# No file carrying the id means the HTTP 200 never became a stored message: NOT_FOUND.
# meiva:link base/lm/harness/antigravity/docs/antigravity.md
def judge(messages: Iterable[QueuedMessage], key: str) -> Reconciliation:
    outcome = Reconciliation.NOT_FOUND
    for message in messages:
        if message.client_message_id != key:
            continue
        if message.delivered:
            return Reconciliation.IN_CONTEXT
        outcome = Reconciliation.PERSISTED_UNDELIVERED
    return outcome
