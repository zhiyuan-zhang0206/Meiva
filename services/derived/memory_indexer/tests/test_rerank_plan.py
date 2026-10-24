# meiva:note
# The rerank stage's text and ordering decisions, from values alone.

from __future__ import annotations

import pytest

from services.derived.memory_indexer.rerank_plan import (
    EXCERPT_CHARS,
    pair_state_text,
    top_indices,
    unit_probability,
)


def test_the_state_leads_with_the_query_then_the_note() -> None:
    state = pair_state_text(
        "how do we deploy",
        path="ops/deploy.md",
        description="Gateway deploy runbook",
        chunk="step one",
        query_header="Search query:",
        note_header="Candidate note:",
    )
    assert state == (
        "Search query:\nhow do we deploy\n\n"
        "Candidate note:\n"
        "path: ops/deploy.md\n"
        "description: Gateway deploy runbook\n"
        "excerpt:\nstep one"
    )


def test_a_note_without_a_description_says_so() -> None:
    state = pair_state_text(
        "q", path="n.md", description="", chunk="c", query_header="Q:", note_header="N:"
    )
    assert "description: (none)\n" in state


def test_the_excerpt_stops_at_the_chunk_size() -> None:
    state = pair_state_text(
        "q",
        path="n.md",
        description="d",
        chunk="z" * (EXCERPT_CHARS + 500),
        query_header="Q:",
        note_header="N:",
    )
    assert state.endswith("excerpt:\n" + "z" * EXCERPT_CHARS)


@pytest.mark.parametrize(
    ("raw", "clamped"), [(-0.2, 0.0), (0.0, 0.0), (0.37, 0.37), (1.0, 1.0), (1.0000001, 1.0)]
)
def test_a_probability_stays_within_the_unit_interval(raw: float, clamped: float) -> None:
    assert unit_probability(raw) == clamped


def test_the_best_scores_come_first_and_ties_keep_candidate_order() -> None:
    assert top_indices([0.2, 0.9, 0.9, 0.5, 0.9], 4) == [1, 2, 4, 3]


def test_k_beyond_the_candidates_returns_them_all() -> None:
    assert top_indices([0.1, 0.8], 5) == [1, 0]
    assert top_indices([], 5) == []
