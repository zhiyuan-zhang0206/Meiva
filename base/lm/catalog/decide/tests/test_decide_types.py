from __future__ import annotations

import pytest

from base.lm.catalog.decide.types import (
    Choice,
    Decision,
    DecisionModelSpec,
    DecisionRequestError,
    DecisionResponseError,
    Score,
    YesNo,
    estimate_tokens,
    not_json_failure,
    question_text,
    require_json_object,
    require_success,
    transport_failure,
)


def test_yes_no_requires_all_three_texts() -> None:
    assert YesNo("Is it spam?", "it is spam", "it is not").outcomes == ("true", "false")
    with pytest.raises(ValueError, match="true_if"):
        YesNo("Is it spam?", " ", "it is not")


def test_choice_needs_two_to_255_described_options() -> None:
    assert Choice("Which?", {"a": "first", "b": "second"}).outcomes == ("a", "b")
    with pytest.raises(ValueError, match=r"2\.\.255"):
        Choice("Which?", {"a": "only one"})
    with pytest.raises(ValueError, match=r"2\.\.255"):
        Choice("Which?", {f"o{i}": "d" for i in range(256)})
    with pytest.raises(ValueError, match="description"):
        Choice("Which?", {"a": "first", "b": ""})


def test_score_needs_aligned_unique_levels_within_bounds() -> None:
    score = Score("How bad?", ("low", "high"), ("fine", "broken"))
    assert score.outcomes == ("low", "high")
    with pytest.raises(ValueError, match="one description per level"):
        Score("How bad?", ("low", "high"), ("fine",))
    with pytest.raises(ValueError, match="unique"):
        Score("How bad?", ("low", "low"), ("a", "b"))
    with pytest.raises(ValueError, match=r"2\.\.10"):
        Score("How bad?", tuple(str(i) for i in range(11)), tuple("d" * 11))
    with pytest.raises(ValueError, match=r"2\.\.10"):
        Score("How bad?", ("only",), ("one",))


def test_decision_probability_rejects_unknown_outcome() -> None:
    decision = Decision(top="true", probabilities={"true": 0.9, "false": 0.1})
    assert decision.probability("true") == 0.9
    with pytest.raises(KeyError, match="maybe"):
        decision.probability("maybe")


def test_model_spec_limits_are_consistent() -> None:
    assert DecisionModelSpec(context_window=10).request_limit == 10
    assert DecisionModelSpec(context_window=10, max_request_tokens=20).request_limit == 20
    with pytest.raises(ValueError, match="positive"):
        DecisionModelSpec(context_window=0)
    with pytest.raises(ValueError, match="max_request_tokens"):
        DecisionModelSpec(context_window=10, max_request_tokens=5)


def test_token_estimate_counts_non_ascii_characters_individually() -> None:
    assert estimate_tokens("abcd" * 10) == 10
    assert estimate_tokens("\u4f60\u597d\u4e16\u754c") == 4
    assert estimate_tokens("") == 0


def test_question_text_carries_every_string() -> None:
    text = question_text(Choice("Which one?", {"alpha": "first thing"}.copy() | {"beta": "second"}))
    for fragment in ("Which one?", "alpha", "first thing", "beta", "second"):
        assert fragment in text


def test_a_non_success_status_is_the_request_failure_with_the_body_head() -> None:
    require_success("m", "https://x", status_code=200, is_success=True, text="ok")
    with pytest.raises(DecisionRequestError, match=r"HTTP 500 from https://x: bolted") as raised:
        require_success("m", "https://x", status_code=500, is_success=False, text="bolted")
    with pytest.raises(DecisionRequestError) as long_body:
        require_success("m", "https://x", status_code=502, is_success=False, text="z" * 400)
    assert len(str(long_body.value)) == len("m: HTTP 502 from https://x: ") + 300
    assert "500" in str(raised.value)


def test_a_transport_error_keeps_its_type_name_and_message() -> None:
    failure = transport_failure("m", "https://x", ValueError("boom"))
    assert isinstance(failure, DecisionRequestError)
    assert str(failure) == "m: request to https://x failed: ValueError: boom"


def test_a_body_that_is_not_json_and_one_that_is_not_an_object() -> None:
    assert isinstance(not_json_failure("m", "https://x"), DecisionResponseError)
    assert str(not_json_failure("m", "https://x")) == "m: response from https://x is not JSON"
    assert require_json_object("m", "https://x", {"a": 1}) == {"a": 1}
    with pytest.raises(DecisionResponseError, match="is not a JSON object"):
        require_json_object("m", "https://x", [1, 2])
