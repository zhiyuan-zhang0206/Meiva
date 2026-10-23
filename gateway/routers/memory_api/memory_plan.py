# meiva:note
# How big one memory search is: values in, the plan out.
#
# A search asks the vector index for `limit` candidates plus one per excluded note (an excluded
# note takes no slot), ranks them with one decision request each, and stops at the tighter of
# the gateway's deadline and the caller's. The endpoint in `memory.py` executes the plan.

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

__all__ = ["SearchPlan", "search_plan"]


# meiva:note
# The sizes and bounds of one search.
@dataclass(frozen=True)
class SearchPlan:
    deadline_s: float
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
    configured_deadline_s: float,
    requested_deadline_s: float | None,
    rerank_candidates: int,
    dedup_candidates: int,
    exclude: Collection[str],
) -> SearchPlan:
    deadline = configured_deadline_s
    if requested_deadline_s is not None:
        deadline = min(deadline, requested_deadline_s)
    limit = dedup_candidates if purpose == "dedup" else rerank_candidates
    excluded = frozenset(exclude)
    return SearchPlan(
        deadline_s=deadline, limit=limit, vector_count=limit + len(excluded), exclude=excluded
    )
