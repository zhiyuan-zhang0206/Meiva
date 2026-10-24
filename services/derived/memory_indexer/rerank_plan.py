# meiva:note
# What the second search stage decides from values: the state text shown to the decision model
# for one candidate, the probability the response contract allows, and which candidates make the
# top `k`. `rerank` reads the notes and asks the model; this module only turns what it was told
# into text and order, so the calibrated wording and the tie-breaking are pinned without a model.

from __future__ import annotations

from collections.abc import Sequence

__all__ = ["EXCERPT_CHARS", "pair_state_text", "top_indices", "unit_probability"]

# meiva:note
# The most characters of a chunk shown to the model (the indexer's chunk size).
EXCERPT_CHARS = 1800


# meiva:note
# The state for one (query, candidate chunk) pair: the query under its lead-in, then the note's
# path, description (`(none)` when empty) and the first `EXCERPT_CHARS` of its chunk.
def pair_state_text(
    query: str,
    *,
    path: str,
    description: str,
    chunk: str,
    query_header: str,
    note_header: str,
) -> str:
    note = (
        f"path: {path}\ndescription: {description or '(none)'}\nexcerpt:\n{chunk[:EXCERPT_CHARS]}"
    )
    return f"{query_header}\n{query}\n\n{note_header}\n{note}"


# meiva:note
# A probability within [0, 1] up to the provider's rounding, clamped so the response contract
# (0..1) holds exactly.
def unit_probability(probability: float) -> float:
    return min(1.0, max(0.0, probability))


# meiva:note
# The indices of the `k` best scores, best first; equal scores keep their candidate order
# (vector-search order), so a tie never reshuffles the results.
def top_indices(scores: Sequence[float], k: int) -> list[int]:
    return sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]
