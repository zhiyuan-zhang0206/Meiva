# meiva:note
# How big one memory search is: values in, the plan out.
#
# A search asks the vector index for `limit` candidates plus one per excluded note (an excluded
# note takes no slot), ranks them with one decision request each, and stops at the tighter of
# the gateway's deadline and the caller's. The backend's own timeout is that deadline plus a
# configured grace, so it is the backstop of the deadline rather than a peer of it. The
# endpoint in `memory.py` executes the plan.

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

__all__ = ["Deadlines", "SearchPlan", "search_plan"]


# meiva:note
# The time budget of one search: the gateway's own deadline, the caller's request deadline
# (None = none), and the grace that keeps the backend's own timeout clear of the deadline's
# timer. The request deadline is the tighter of the two; the backend's timeout is that plus the
# grace, so a loaded gateway never has both timers due in one scheduling turn — in that turn the
# backend's inner scope absorbs the cancellation and answers with its own message-less timeout
# instead of the message naming the deadline.
@dataclass(frozen=True)
class Deadlines:
    configured_s: float
    requested_s: float | None
    backend_grace_s: float

    @property
    def request_s(self) -> float:
        if self.requested_s is None:
            return self.configured_s
        return min(self.configured_s, self.requested_s)

    @property
    def backend_s(self) -> float:
        return self.request_s + self.backend_grace_s


# meiva:note
# The sizes and bounds of one search.
@dataclass(frozen=True)
class SearchPlan:
    deadlines: Deadlines
    limit: int
    vector_count: int
    exclude: frozenset[str]


# meiva:note
# The plan for one search.
#
# `limit` is the candidate count of the purpose (dedup has its own): a `k` above it returns
# the candidates, and the number of decision requests never grows with `k`. The vector
# query is widened by the exclusion list so the excluded notes cost no candidate slot.
def search_plan(
    *,
    purpose: Literal["recall", "search", "dedup"],
    deadlines: Deadlines,
    rerank_candidates: int,
    dedup_candidates: int,
    exclude: Collection[str],
) -> SearchPlan:
    limit = dedup_candidates if purpose == "dedup" else rerank_candidates
    excluded = frozenset(exclude)
    return SearchPlan(
        deadlines=deadlines,
        limit=limit,
        vector_count=limit + len(excluded),
        exclude=excluded,
    )
