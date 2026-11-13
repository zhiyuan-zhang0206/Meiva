from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol

from base.lm.harness.session.transcript.entries import InputRow

CLAIM_EARLY = timedelta(seconds=5)
CLAIM_SLACK = timedelta(minutes=10)


class Claimed(Protocol):
    @property
    def id(self) -> int: ...
    @property
    def content(self) -> str: ...
    @property
    def created_at(self) -> datetime | None: ...
    @property
    def claimed_at(self) -> datetime | None: ...


# meiva:note
# An inbox row belongs to the earliest input in file order whose USER_REQUEST contains its content
# and was written within the claim window. A batch may carry several rows; equal text alone is not identity.
def assign_inputs[R: Claimed](
    rows: Sequence[R], inputs: Mapping[str, InputRow]
) -> dict[str, list[R]]:
    assigned: dict[str, list[R]] = {}
    epoch = datetime.min.replace(tzinfo=UTC)
    for row in sorted(rows, key=lambda item: (item.created_at or epoch, item.id)):
        claimed, text = row.claimed_at, row.content.strip()
        if claimed is None or not text:
            continue
        for key, held in inputs.items():
            if (
                held.text is not None
                and claimed - CLAIM_EARLY <= held.stamp <= claimed + CLAIM_SLACK
                and text in held.text
            ):
                assigned.setdefault(key, []).append(row)
                break
    return assigned
