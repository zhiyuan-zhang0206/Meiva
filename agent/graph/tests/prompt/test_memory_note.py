# meiva:note
# What passive memory recall injects and how the note reads, decided from search hits.
from __future__ import annotations

from agent.graph.recall.memory_note import qualifying, render_note_text
from base.agents.context.gateway_client.memory_search import MemorySearchResult


def _hit(
    path: str, *, score: float = 0.9, description: str = "", chunk: str = ""
) -> MemorySearchResult:
    return MemorySearchResult(path, description, ("type/user",), score, chunk)


def test_best_scores_first_at_or_above_the_threshold() -> None:
    results = [_hit("lo.md", score=0.69), _hit("hi.md", score=0.95), _hit("edge.md", score=0.7)]

    picked = qualifying(results, threshold=0.7, limit=5, present={"lo.md", "hi.md", "edge.md"})

    assert [h.path for h in picked] == ["hi.md", "edge.md"]


def test_the_cap_keeps_the_best_whatever_order_search_returned() -> None:
    scores = {"a.md": 0.75, "b.md": 0.99, "c.md": 0.8, "d.md": 0.9}
    results = [_hit(path, score=score) for path, score in scores.items()]

    picked = qualifying(results, threshold=0.0, limit=2, present=set(scores))

    assert [h.path for h in picked] == ["b.md", "d.md"]


def test_a_path_not_synced_here_is_skipped_and_takes_no_slot() -> None:
    results = [_hit("not-yet.md", score=0.99), _hit("here.md", score=0.8)]

    picked = qualifying(results, threshold=0.5, limit=1, present={"here.md"})

    assert [h.path for h in picked] == ["here.md"]


def test_nothing_qualifies_below_the_threshold() -> None:
    assert qualifying([_hit("a.md", score=0.3)], threshold=0.72, limit=3, present={"a.md"}) == []


def test_a_zero_limit_picks_nothing() -> None:
    assert qualifying([_hit("a.md")], threshold=0.0, limit=0, present={"a.md"}) == []


def test_note_lists_path_score_description_and_matched_passage() -> None:
    picked = [
        _hit("a.md", score=0.83, description="deploy runbook", chunk="restart the gateway"),
        _hit("sub/b.md", score=0.81, description="gateway ports", chunk="line one\n\nline three"),
    ]

    text = render_note_text(picked)

    assert "- a.md (score 0.83): deploy runbook\n  > restart the gateway" in text
    assert "- sub/b.md (score 0.81): gateway ports\n  > line one\n  >\n  > line three" in text
    assert text.index("a.md") < text.index("sub/b.md")


def test_tags_are_not_rendered() -> None:
    assert "type/user" not in render_note_text([_hit("a.md", description="d", chunk="c")])


def test_an_empty_description_is_not_synthesized() -> None:
    text = render_note_text([_hit("c.md", score=0.8)])

    assert "- c.md (score 0.80)" in text.splitlines()


def test_a_passage_equal_to_the_description_is_not_repeated() -> None:
    text = render_note_text([_hit("a.md", description="one-liner", chunk="one-liner")])

    assert text.count("one-liner") == 1
