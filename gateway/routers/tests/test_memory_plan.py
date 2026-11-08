# meiva:note
# How big one memory search is: values in, the plan out.

from dataclasses import replace

from gateway.routers.memory_api.memory_plan import Deadlines, SearchPlan, search_plan

BUDGET = Deadlines(configured_s=15.0, requested_s=None, backend_grace_s=0.5)


def test_search_and_recall_rank_the_shared_candidate_count() -> None:
    for purpose in ("search", "recall"):
        plan = search_plan(
            purpose=purpose,
            deadlines=BUDGET,
            rerank_candidates=30,
            dedup_candidates=5,
            exclude=(),
        )
        assert plan == SearchPlan(deadlines=BUDGET, limit=30, vector_count=30, exclude=frozenset())


def test_dedup_ranks_its_own_smaller_candidate_count() -> None:
    plan = search_plan(
        purpose="dedup",
        deadlines=BUDGET,
        rerank_candidates=30,
        dedup_candidates=5,
        exclude=(),
    )

    assert plan.limit == 5


def test_an_excluded_note_widens_the_vector_query_but_takes_no_candidate_slot() -> None:
    plan = search_plan(
        purpose="search",
        deadlines=BUDGET,
        rerank_candidates=30,
        dedup_candidates=5,
        exclude=["a.md", "b.md", "a.md"],
    )

    assert plan.exclude == {"a.md", "b.md"}
    assert (plan.limit, plan.vector_count) == (30, 32)


def test_the_tighter_deadline_wins() -> None:
    sooner = replace(BUDGET, requested_s=0.3)
    later = replace(BUDGET, requested_s=600.0)

    assert (sooner.request_s, later.request_s) == (0.3, 15.0)


# meiva:note
# The backend is the backstop: its own timeout expires strictly after the request's, and the tied
# case is the one the grace removes.
def test_the_backend_timeout_is_the_request_deadline_plus_the_grace() -> None:
    sooner = replace(BUDGET, requested_s=0.3)
    tied = replace(sooner, backend_grace_s=0.0)

    assert (sooner.request_s, sooner.backend_s) == (0.3, 0.8)
    assert tied.backend_s == tied.request_s
