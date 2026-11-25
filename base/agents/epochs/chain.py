# meiva:link base/agents/epochs/docs/native-slice.md

from __future__ import annotations

from collections.abc import Sequence

from base.agents.epochs.model import HarnessEpoch

MIN_EPOCHS = 2


def usable_epochs(rows: Sequence[HarnessEpoch], harness: str) -> list[HarnessEpoch]:
    if len(rows) < MIN_EPOCHS:
        return []
    if [row.seq for row in rows] != list(range(len(rows))):
        return []
    if rows[-1].ended_at is not None or rows[-1].harness != harness:
        return []
    return list(rows)
