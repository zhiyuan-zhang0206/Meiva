# meiva:note
# The key an inbox row is handed to an external harness under. The harness records an input by an id
# derived from this key (`base/lm/harness/claude/delivery.py::key_uuid`), and the key is how a later
# process finds the row in that record again (reconciliation, the timeline projection).
#
# One rule: a row is never handed over again under a key the harness has already recorded. Claude
# Code drops a user message whose uuid is already in its session without a word: it echoes the
# message back and then runs no turn and sends no result (2026-11-19, task #7423: rows a quota abort
# returned to the queue were handed to the resumed CLI under their old key, the CLI dropped them,
# the foreground waited for a turn that never came, and every message behind them was stuck). A row
# a halted turn handed back (`HANDBACK_COUNT_FIELD`, counted once per handback) is therefore handed
# over under a key that names the count. A row returned because the harness never took it (a
# refused, lost or undelivered input) keeps its key: the harness has no record of it.
#
# A batch handed over as one input is named by its group key (`INPUT_KEY_FIELD`), written on the
# claimed rows when the batch is handed over and removed whenever its rows go back to the queue. It
# names an input already handed over, so it is read only to find that input again
# (`recorded_input_key`), never to hand a row over (`row_input_key`).
# meiva:link base/lm/harness/docs/delivery.md

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

INPUT_KEY_FIELD: Final = "harness_input_key"

# meiva:note
# The payload key counting the halted harness turns a row was handed to and handed back from (a
# quota exhaustion, a stalled turn). The stored name predates the stall abort; rows already in the
# queue carry it, so it is kept.
HANDBACK_COUNT_FIELD: Final = "harness_quota_retries"


def handback_count(payload: Mapping[str, object] | None) -> int:
    raw = payload.get(HANDBACK_COUNT_FIELD) if payload else None
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return max(raw, 0)
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return 0


# meiva:note
# The key a row is handed over under when it is handed over on its own or names the batch it leads.
def row_input_key(row_id: int, payload: Mapping[str, object] | None) -> str:
    count = handback_count(payload)
    return f"inbound-{row_id}" if count == 0 else f"inbound-{row_id}-handback-{count}"


# meiva:note
# The key of the input a claimed or settled row was handed over in: its batch's group key when it rode
# in one, its own key otherwise.
def recorded_input_key(row_id: int, payload: Mapping[str, object] | None) -> str:
    grouped = payload.get(INPUT_KEY_FIELD) if payload else None
    if isinstance(grouped, str) and grouped:
        return grouped
    return row_input_key(row_id, payload)
