# meiva:note
# The reply-suggestion decisions, asserted on values.

from __future__ import annotations

import json

import pytest

from base.lm.catalog import ModelCatalog
from gateway.agents.reply_suggestion.plan import (
    ReplySuggestionPolicy,
    draft_instruction,
    draft_verdict,
    gate_refusal,
    model_problem,
    refusal_before_reading,
)
from gateway.agents.reply_suggestion.schemas import MAX_SUGGESTION_CHARS
from gateway.agents.reply_suggestion.tests.support import default_policy


@pytest.mark.parametrize(
    ("enabled", "idle", "reason"),
    [
        (False, True, "disabled"),
        (False, False, "disabled"),
        (True, False, "agent_not_idle"),
        (True, True, None),
    ],
)
def test_the_conversation_is_read_only_for_an_enabled_idle_agent(
    enabled: bool, idle: bool, reason: str | None
) -> None:
    assert refusal_before_reading(enabled=enabled, idle=idle) == reason


def test_the_draft_instruction_states_the_configured_bound() -> None:
    assert "never more than 2" in draft_instruction(2)
    assert "never more than 3" in draft_instruction(3)


def test_a_gate_below_the_threshold_refuses_and_one_at_it_passes() -> None:
    assert gate_refusal(0.2, 0.5) == "not_useful"
    assert gate_refusal(0.5, 0.5) is None
    assert gate_refusal(0.9, 0.5) is None


def policy(**changes: object) -> ReplySuggestionPolicy:
    # meiva:note
    # The keyword arguments are spread from a dict of changed fields, which pyright cannot match to the signature.
    return ReplySuggestionPolicy(**{**default_policy().__dict__, **changes})  # pyright: ignore[reportArgumentType]


def test_the_default_models_belong_to_their_catalogs(model_catalog: ModelCatalog) -> None:
    assert model_problem(policy(), model_catalog.models, model_catalog.decision_models) is None


def test_a_chat_model_cannot_be_the_gate(model_catalog: ModelCatalog) -> None:
    problem = model_problem(
        policy(gate_model="deepseek-flash"), model_catalog.models, model_catalog.decision_models
    )
    assert problem is not None
    assert "reply_suggestion_gate_model" in problem
    assert "not a registered decision model" in problem


def test_a_decision_model_cannot_draft(model_catalog: ModelCatalog) -> None:
    problem = model_problem(
        policy(model="jev-1.13.0"), model_catalog.models, model_catalog.decision_models
    )
    assert problem is not None
    assert "reply_suggestion_model" in problem
    assert "not a registered chat model" in problem


def test_an_unknown_gate_model_is_named(model_catalog: ModelCatalog) -> None:
    problem = model_problem(
        policy(gate_model="jev-0.0.0"), model_catalog.models, model_catalog.decision_models
    )
    assert problem is not None
    assert "jev-0.0.0" in problem


def test_a_disabled_feature_does_not_check_its_models(model_catalog: ModelCatalog) -> None:
    unchecked = policy(enabled=False, model="nope", gate_model="nope")
    assert model_problem(unchecked, model_catalog.models, model_catalog.decision_models) is None


def verdict(text: str, parsed: object, **changes: object):
    options: dict[str, object] = {
        "finished": True,
        "parse_failed": False,
        "max_candidates": 3,
        **changes,
    }
    # meiva:note
    # The keyword arguments are spread from a dict of changed fields, which pyright cannot match to the signature.
    return draft_verdict(text=text, parsed=parsed, **options)  # pyright: ignore[reportArgumentType]


def drafted(candidates: object) -> tuple[str, object]:
    document = {"suggestions": candidates}
    return json.dumps(document), document


def test_a_finished_valid_draft_keeps_its_candidates_in_order() -> None:
    result = verdict(*drafted(["  Yes, deploy it.  ", "Wait, explain the plan first."]))
    assert (result.suggestions, result.rejection) == (
        ["Yes, deploy it.", "Wait, explain the plan first."],
        None,
    )


@pytest.mark.parametrize("candidates", [["Continue."], ["Continue.", "Pause."], ["A.", "B.", "C."]])
def test_one_to_the_bound_candidates_all_reach_the_response(candidates: list[str]) -> None:
    assert verdict(*drafted(candidates)).suggestions == candidates


def test_more_candidates_than_the_bound_are_rejected_as_a_whole() -> None:
    result = verdict(*drafted(["A.", "B.", "C.", "D."]))
    assert (result.suggestions, result.rejection) == ([], "too_many")
    assert verdict(*drafted(["A.", "B.", "C."]), max_candidates=2).rejection == "too_many"


@pytest.mark.parametrize(
    "candidates",
    [
        [],
        ["   \n"],
        ["x" * (MAX_SUGGESTION_CHARS + 1)],
        ["Yes", " yes "],
        ["Yes", 42],
    ],
)
def test_invalid_candidates_are_rejected_as_a_whole(candidates: object) -> None:
    assert verdict(*drafted(candidates)).rejection == "invalid"


@pytest.mark.parametrize(
    "document", [["Yes"], {}, {"suggestions": ["Yes"], "unexpected": "extra"}, "plain"]
)
def test_a_document_of_the_wrong_shape_is_rejected(document: object) -> None:
    assert verdict(json.dumps(document), document).rejection == "invalid"


def test_text_that_is_not_json_is_rejected_even_when_a_parser_repaired_it() -> None:
    truncated = '{"suggestions": ["Use A."]'
    assert verdict(truncated, {"suggestions": ["Use A."]}).rejection == "invalid"
    assert verdict("not JSON", None).rejection == "invalid"
    assert verdict("", None).rejection == "invalid"


@pytest.mark.parametrize(
    ("finished", "parse_failed"), [(False, False), (True, True), (False, True)]
)
def test_an_unfinished_or_unparsed_draft_is_rejected_even_if_its_text_is_valid(
    finished: bool, parse_failed: bool
) -> None:
    result = verdict(*drafted(["Yes."]), finished=finished, parse_failed=parse_failed)
    assert (result.suggestions, result.rejection) == ([], "unfinished")
