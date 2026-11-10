from __future__ import annotations

from collections.abc import Iterable, Mapping

from base.host.boundary.json_values import is_object_dict
from base.lm.harness.delivery import Reconciliation


# meiva:note
# Decide from a Codex rollout whether one input reached the model. The rollout records each
# `userMessage` item as an `event_msg` row of type `item_completed` whose item carries `client_id` — the
# `clientUserMessageId` the input was sent with — and the row is written in the same millisecond as
# the live notification, so it is the durable point: a live receipt that never arrived does not mean
# the thread lacks the message, and a live notification that did arrive may not have been written
# yet. Rows are already-parsed JSON objects: reading the file is `rollout.py`'s job. Codex records
# nothing between "received" and "in context", so there is no persisted-but-undelivered answer.
# The rollout format is Codex's own and not a published interface; this was observed on app-server
# 0.166.
# meiva:link base/lm/harness/codex/docs/codex.md
def judge(rows: Iterable[Mapping[str, object]], key: str) -> Reconciliation:
    for row in rows:
        if row.get("type") != "event_msg":
            continue
        payload = row.get("payload")
        if not is_object_dict(payload) or payload.get("type") != "item_completed":
            continue
        item = payload.get("item")
        if is_object_dict(item) and item.get("client_id") == key:
            return Reconciliation.IN_CONTEXT
    return Reconciliation.NOT_FOUND
