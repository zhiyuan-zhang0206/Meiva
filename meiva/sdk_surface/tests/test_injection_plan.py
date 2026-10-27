# meiva:note
# `meiva/sdk_surface/pure/media/injection/injection_plan.py` unit tests for the pieces the checker used to
# decide inline: the token budget of a piece, how the answers of the pieces fold into a verdict,
# and the refusal for a model that is not a decision model. Asserted by value; the end-to-end
# checks stay in `meiva/tests/namespaces/sdk_surface/test_injection_check.py`.

from __future__ import annotations

import pytest

from base.lm.catalog.decide.types import (
    Decision,
    DecisionKeyMissingError,
    DecisionRequestError,
)
from meiva.sdk_surface.pure.media.injection.injection_pieces import (
    chunk_budget,
    fold_outcomes,
    pieces_to_judge,
    split_for_model,
)
from meiva.sdk_surface.pure.media.injection.injection_plan import unknown_model_message

_QID = "page_addresses_ai"


def _answer(true_probability: float) -> dict[str, Decision]:
    return {
        _QID: Decision(
            top="true" if true_probability >= 0.5 else "false",
            probabilities={"true": true_probability, "false": 1 - true_probability},
        )
    }


def test_the_budget_is_the_window_less_the_question_and_the_margin() -> None:
    assert chunk_budget(context_window=8192, question_tokens=300, margin_tokens=512) == 7380


def test_a_window_the_question_and_margin_fill_gives_a_budget_the_splitter_refuses() -> None:
    budget = chunk_budget(context_window=800, question_tokens=300, margin_tokens=512)
    assert budget == -12
    with pytest.raises(ValueError, match="max_tokens must be at least 1"):
        split_for_model("page", budget)


def test_the_highest_answering_piece_decides() -> None:
    verdicts = fold_outcomes([_answer(0.1), _answer(0.9), _answer(0.4)], question_id=_QID)
    assert verdicts.probabilities == (0.1, 0.9, 0.4)
    assert verdicts.highest() == 0.9
    assert verdicts.failures == ()
    assert verdicts.hard is None


def test_no_answer_is_no_verdict() -> None:
    assert fold_outcomes([], question_id=_QID).highest() is None


def test_a_failed_piece_is_kept_for_the_log_and_does_not_hide_another_piece() -> None:
    failure = DecisionRequestError("provider down")
    verdicts = fold_outcomes([failure, _answer(0.8)], question_id=_QID)
    assert verdicts.failures == (failure,)
    assert verdicts.highest() == 0.8
    assert verdicts.hard is None


def test_every_piece_failing_is_no_verdict_but_not_an_error() -> None:
    first, second = DecisionRequestError("down"), DecisionKeyMissingError("no key")
    verdicts = fold_outcomes([first, second], question_id=_QID)
    assert verdicts.failures == (first, second)
    assert verdicts.highest() is None
    assert verdicts.hard is None


def test_an_error_that_is_not_a_decision_failure_stops_the_fold_at_its_piece() -> None:
    early = DecisionRequestError("early")
    late = DecisionRequestError("late")
    bug = KeyError("bug")
    verdicts = fold_outcomes([_answer(0.3), early, bug, late, _answer(0.99)], question_id=_QID)
    assert verdicts.hard is bug
    assert verdicts.probabilities == (0.3,)
    assert verdicts.failures == (early,)


def test_an_answer_without_the_question_stops_the_fold_but_keeps_the_failures_before_it() -> None:
    early = DecisionRequestError("early")
    malformed: dict[str, Decision] = {"other": Decision(top="true", probabilities={"true": 1.0})}
    verdicts = fold_outcomes([_answer(0.3), early, malformed, _answer(0.9)], question_id=_QID)
    assert isinstance(verdicts.hard, KeyError)
    assert verdicts.probabilities == (0.3,)
    assert verdicts.failures == (early,)


def test_an_answer_that_lacks_the_true_outcome_stops_the_fold_too() -> None:
    no_true: dict[str, Decision] = {_QID: Decision(top="false", probabilities={"false": 1.0})}
    verdicts = fold_outcomes([no_true], question_id=_QID)
    assert isinstance(verdicts.hard, KeyError)
    assert verdicts.probabilities == ()


def test_the_unknown_model_refusal_names_the_model_and_lists_the_decision_models_sorted() -> None:
    message = unknown_model_message("gpt-x", ["jev-2", "jev-1"])
    assert message == (
        "web_fetch_injection_model 'gpt-x' is not a decision model; available: jev-1, jev-2"
    )


def test_blank_pieces_are_not_judged() -> None:
    text = "alpha\n" + " " * 40 + "\n" + "beta\n"
    assert any(not piece.strip() for piece in split_for_model(text, 4))
    judged = pieces_to_judge(text, 4)
    assert judged
    assert all(piece.strip() for piece in judged)
    assert "".join(judged).replace(" ", "").replace("\n", "") == "alphabeta"


def test_a_page_of_whitespace_has_nothing_to_judge() -> None:
    assert pieces_to_judge(" \n\t " * 20, 10) == []
