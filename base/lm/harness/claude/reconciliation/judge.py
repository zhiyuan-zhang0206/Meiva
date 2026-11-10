from __future__ import annotations

from collections.abc import Iterable, Mapping

from base.host.boundary.json_values import is_object_dict
from base.lm.harness.claude.delivery import key_uuid
from base.lm.harness.delivery import Reconciliation


# meiva:note
# Decide from a Claude Code transcript whether one input reached the model. The input was sent with
# the uuid `key_uuid(key)`, and the CLI writes it to the transcript in one of two ways: as a `user` row
# whose `uuid` is ours (an input that started a turn), or as an `attachment` row of type
# `queued_command` whose `source_uuid` is ours (an input absorbed at a tool boundary). Either row is
# the durable point. The live echo is not: an echo can be seen in the instant before a kill that the
# transcript has not yet recorded, and after a resume the model does not know that message. A
# `queue-operation enqueue` row carries no uuid and, after a crash, the resumed CLI does not
# deliver it (observed on CLI 2.1.301), so it proves nothing and the answer for such an input is
# NOT_FOUND. The transcript format is the CLI's own, not a published interface.
# meiva:link base/lm/harness/claude/docs/claude.md
def judge(rows: Iterable[Mapping[str, object]], key: str) -> Reconciliation:
    wanted = key_uuid(key)
    for row in rows:
        if row.get("type") == "user" and row.get("uuid") == wanted:
            return Reconciliation.IN_CONTEXT
        attachment = row.get("attachment")
        if (
            row.get("type") == "attachment"
            and is_object_dict(attachment)
            and attachment.get("type") == "queued_command"
            and attachment.get("source_uuid") == wanted
        ):
            return Reconciliation.IN_CONTEXT
    return Reconciliation.NOT_FOUND
