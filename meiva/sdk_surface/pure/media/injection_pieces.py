# meiva:note
# How a fetched page is cut into model-sized pieces and how the answers of the pieces fold into
# one verdict, over values: the cut points, the token budget of a piece and the highest answer.
# The checker (`injection_check.py`) calls the decision model once per piece; these functions
# only choose, so each is tested directly.
# meiva:link meiva/docs/sdk-surface/sdk-surface.meiva.okf.md

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from base.lm.catalog.decide.types import Decision, DecisionError


# meiva:note
# Split `text` into consecutive pieces of at most `max_tokens` by `estimate_tokens`. A cut falls
# on a line break when one exists in the last fifth of the piece, otherwise mid-line. The pieces
# joined give back `text`. A budget below one token is a caller bug and raises ValueError.
def split_for_model(text: str, max_tokens: int) -> list[str]:
    if max_tokens < 1:
        raise ValueError(f"max_tokens must be at least 1, got {max_tokens}")
    # meiva:note
    # estimate_tokens = ceil(ascii / 4) + non-ascii, so in quarter-token units an ASCII
    # character costs 1 and any other character 4.
    limit = max_tokens * 4
    pieces: list[str] = []
    start = 0
    while start < len(text):
        used = 0
        end = start
        while end < len(text):
            cost = 1 if ord(text[end]) < 128 else 4
            if used + cost > limit:
                break
            used += cost
            end += 1
        if end < len(text):
            newline = text.rfind("\n", start + (end - start) * 4 // 5, end)
            if newline > start:
                end = newline + 1
        pieces.append(text[start:end])
        start = end
    return pieces


# meiva:note
# The pieces of `text` worth a request: `split_for_model` without the blank ones, so a stretch of
# whitespace between two pieces never costs a model call.
def pieces_to_judge(text: str, max_tokens: int) -> list[str]:
    return [piece for piece in split_for_model(text, max_tokens) if piece.strip()]


# meiva:note
# The tokens one piece of the page may hold: the model's context window less the question and a
# margin. `estimate_tokens` is the pre-flight estimate, not the provider's count, so the margin
# leaves room. A window the question and margin already fill yields a budget below one, which
# `split_for_model` refuses.
def chunk_budget(*, context_window: int, question_tokens: int, margin_tokens: int) -> int:
    return context_window - question_tokens - margin_tokens


# meiva:note
# What the answers of a page's pieces came to, in piece order. `probabilities` are the answers
# that arrived, `failures` the decision failures met before any hard error, and `hard` the first
# outcome that is not a decision failure: the caller logs the failures, then raises `hard`.
@dataclass(frozen=True, slots=True)
class PieceVerdicts:
    probabilities: tuple[float, ...]
    failures: tuple[DecisionError, ...]
    hard: BaseException | None

    # meiva:note
    # The highest probability over the pieces that answered; None when none did.
    def highest(self) -> float | None:
        return max(self.probabilities) if self.probabilities else None


# meiva:note
# Fold the outcomes of the per-piece requests (one entry per piece, in order) into a verdict.
# A decision failure is kept for the log and costs only its own piece; any other exception stops
# the fold, so it is not swallowed and the pieces after it are not read. An answer contributes the
# probability of its `question_id` being "true"; an answer that lacks it is such an exception,
# handed back as `hard` with the failures before it still in order.
def fold_outcomes(
    outcomes: Sequence[Mapping[str, Decision] | BaseException], *, question_id: str
) -> PieceVerdicts:
    probabilities: list[float] = []
    failures: list[DecisionError] = []
    for outcome in outcomes:
        if isinstance(outcome, DecisionError):
            failures.append(outcome)
        elif isinstance(outcome, BaseException):
            return PieceVerdicts(tuple(probabilities), tuple(failures), outcome)
        else:
            try:
                probabilities.append(outcome[question_id].probability("true"))
            except Exception as exc:
                return PieceVerdicts(tuple(probabilities), tuple(failures), exc)
    return PieceVerdicts(tuple(probabilities), tuple(failures), None)
