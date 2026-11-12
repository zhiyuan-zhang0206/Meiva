from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from base.lm.harness.codex.transcript.clock import iso_at
from base.lm.harness.session.transcript.entries import Order

# meiva:note
# Where a thread item sits and what every entry made from it carries: the turn and record it belongs to, when it
# started and (if it has) finished, and its position in the caller's sequence. `Place.common` is the base of every
# entry an item yields: its key is the entry kind and the item id, never the position, so a history item and its
# rollout twin get the same key.
# meiva:link base/lm/harness/docs/transcript-projection.md


class Common(TypedDict):
    key: str
    order: Order
    record: str
    turn: str | None
    at: str | None


# meiva:note
# Where an item sits: the turn and record it belongs to, when it started and (if it has) finished, and
# its position in the caller's sequence.
@dataclass(frozen=True, slots=True)
class Where:
    turn: str
    record: str
    started_ms: int
    completed_ms: int | None
    seq: int


@dataclass(frozen=True, slots=True)
class Place:
    id: str
    where: Where
    joined: bool

    @property
    def turn(self) -> str:
        return self.where.turn

    @property
    def started_ms(self) -> int:
        return self.where.started_ms

    @property
    def completed_ms(self) -> int | None:
        return self.where.completed_ms

    def common(self, kind: str, slot: int = 0, *, at_ms: int | None = None) -> Common:
        return Common(
            key=f"{kind}:{self.id}",
            order=Order(self.where.seq, slot),
            record=self.where.record,
            turn=self.turn,
            at=iso_at(self.started_ms if at_ms is None else at_ms),
        )

    def elapsed_ms(self) -> int | None:
        if self.completed_ms is None:
            return None
        return max(self.completed_ms - self.started_ms, 0)
