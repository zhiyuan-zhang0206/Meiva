from __future__ import annotations

from collections.abc import Sequence

from agent.harness_timeline.plan import WritePlan
from base.host.boundary.recent import RecentMap
from base.lm.harness.events import Usage
from base.lm.harness.session.transcript.entries import MAIN_RECORD

CLOSED_KEPT = 256
DELIVERED_KEPT = 1_024
TURN_USAGE_LIMIT = 256


# meiva:note
# What the writer knows about record turns, by identity: the record turn each of our inputs was delivered in (from the row
# that settled it), the spend tallied for each turn still open, and the spend of each turn that closed. A pairing of the
# stream's turn with the record's closing asks it `turns_closed(keys)` and `closed_usage(keys)` with the keys of the inputs
# that were in the turn, so a late closing, a lost result frame or a turn the CLI ran on its own never shifts which turn
# another is compared with. Each map is the latest N, so it does not grow with the conversation.
# meiva:link agent/harness_timeline/README.md
class TurnLedger:
    def __init__(self) -> None:
        self._closed: RecentMap[str, Usage | None] = RecentMap(CLOSED_KEPT)
        self._delivered: RecentMap[str, str] = RecentMap(DELIVERED_KEPT)
        self._open_usage: RecentMap[str, Usage] = RecentMap(TURN_USAGE_LIMIT)

    def sizes(self) -> dict[str, int]:
        return {
            "turn_usage": len(self._open_usage),
            "closed": len(self._closed),
            "delivered": len(self._delivered),
        }

    def record(self, plan: WritePlan) -> None:
        for spend in plan.usage:
            if spend.record == MAIN_RECORD and spend.turn is not None:
                self._open_usage[spend.turn] = (
                    self._open_usage.get(spend.turn) or Usage()
                ) + spend.usage
        for settlement in plan.settlements:
            if settlement.turn is not None:
                self._delivered[settlement.input_key] = settlement.turn
        for closed in plan.closed:
            self._closed[closed.turn] = self._open_usage.pop(closed.turn)

    def turn_of(self, key: str) -> str | None:
        return self._delivered.get(key)

    def turns_closed(self, keys: Sequence[str]) -> bool:
        turns = [self._delivered.get(key) for key in keys]
        return all(turn is not None and turn in self._closed for turn in turns)

    def closed_usage(self, keys: Sequence[str]) -> Usage | None:
        total: Usage | None = None
        for turn in dict.fromkeys(t for t in (self._delivered.get(key) for key in keys) if t):
            if turn in self._closed and (spend := self._closed.get(turn)) is not None:
                total = spend if total is None else total + spend
        return total
