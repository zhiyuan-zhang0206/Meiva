from __future__ import annotations

from enum import StrEnum


# meiva:note
# What a harness's own durable record says about one input, asked after a crash or a restart when
# the live acknowledgements are gone. IN_CONTEXT: the model has it (or will, with no further
# delivery). PERSISTED_UNDELIVERED: the harness stored it and will deliver it by itself, so it must
# not be delivered again. NOT_FOUND: the record has no trace of it, so it is safe to re-deliver.
# No harness de-duplicates, which is why only NOT_FOUND allows a re-delivery.
# meiva:link base/lm/harness/docs/delivery.md
class Reconciliation(StrEnum):
    IN_CONTEXT = "in_context"
    PERSISTED_UNDELIVERED = "persisted_undelivered"
    NOT_FOUND = "not_found"
