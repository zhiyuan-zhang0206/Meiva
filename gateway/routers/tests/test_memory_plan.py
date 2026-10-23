# meiva:note
# How big one memory search is: values in, the plan out.

from gateway.routers.memory_api.memory_plan import SearchPlan, search_plan

BASE = {
    "configured_deadline_s": 15.0,
    "requested_deadline_s": None,
    "rerank_candidates": 30,
    "dedup_candidates": 5,
    "exclude": (),
}


def test_search_and_recall_rank_the_shared_candidate_count() -> None:
    for purpose in ("search", "recall"):
        # meiva:note
        # The keyword arguments are spread from a dict of changed fields, which pyright cannot match to the signature.
        plan = search_plan(purpose=purpose, **BASE)  # pyright: ignore[reportArgumentType]
        assert plan == SearchPlan(deadline_s=15.0, limit=30, vector_count=30, exclude=frozenset())


def test_dedup_ranks_its_own_smaller_candidate_count() -> None:
    # meiva:note
    # The keyword arguments are spread from a dict of changed fields, which pyright cannot match to the signature.
    assert search_plan(purpose="dedup", **BASE).limit == 5  # pyright: ignore[reportArgumentType]


def test_an_excluded_note_widens_the_vector_query_but_takes_no_candidate_slot() -> None:
    # meiva:note
    # The keyword arguments are spread from a dict of changed fields, which pyright cannot match to the signature.
    plan = search_plan(purpose="search", **{**BASE, "exclude": ["a.md", "b.md", "a.md"]})  # pyright: ignore[reportArgumentType]
    assert plan.exclude == {"a.md", "b.md"}
    assert (plan.limit, plan.vector_count) == (30, 32)


def test_the_tighter_deadline_wins() -> None:
    # meiva:note
    # The keyword arguments are spread from a dict of changed fields, which pyright cannot match to the signature.
    sooner = search_plan(purpose="search", **{**BASE, "requested_deadline_s": 0.3})  # pyright: ignore[reportArgumentType]
    later = search_plan(purpose="search", **{**BASE, "requested_deadline_s": 600.0})  # pyright: ignore[reportArgumentType]
    assert (sooner.deadline_s, later.deadline_s) == (0.3, 15.0)
