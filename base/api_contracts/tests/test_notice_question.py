import pytest

from base.api_contracts.notice_question import (
    NoticeAnswer,
    answer_reply,
    parse_numbered_answer,
    parse_question,
    validate_answer,
)


def choice(kind: str = "multi_select", *, other: bool = False):
    return parse_question(
        {
            "type": kind,
            "options": [{"id": "web", "label": "Web"}, {"id": "desktop", "label": "Desktop"}],
            "allow_other": other,
        }
    )


@pytest.mark.parametrize("kind", ["single_select", "multi_select"])
def test_choice_comment_does_not_select_mentioned_options(kind: str) -> None:
    question = choice(kind)
    answer = parse_numbered_answer(question, "2\nBase this on option 2, with option 1 deferred.")
    assert answer.option_ids == ["desktop"]
    assert answer.comment == "Base this on option 2, with option 1 deferred."
    rendered = answer_reply(question, answer)
    assert "1. Web\n2. Desktop" in rendered
    assert "Selected: 2" in rendered
    assert "Comment: Base this on option 2" in rendered
    assert "Other" not in rendered


def test_multi_select_and_comment_keep_canonical_order() -> None:
    question = choice()
    answer = validate_answer(
        question,
        NoticeAnswer(option_ids=["desktop", "web"], comment="More context"),
    )
    assert answer.option_ids == ["web", "desktop"]
    assert "Selected: 1, 2" in answer_reply(question, answer)
    assert "Comment: More context" in answer_reply(question, answer)


def test_numbered_reply_without_other_preserves_following_lines_as_note() -> None:
    answer = parse_numbered_answer(choice(), "2,1\nUse option 2 first.\nThen option 1.")
    assert answer.option_ids == ["web", "desktop"]
    assert answer.other_text is None
    assert answer.comment == "Use option 2 first.\nThen option 1."


@pytest.mark.parametrize("reply", ["", "yes", "0", "4", "1,1", "3", "3\n  "])
def test_numbered_choice_reply_fails_closed(reply: str) -> None:
    with pytest.raises(ValueError):
        parse_numbered_answer(choice(), reply)


@pytest.mark.parametrize(
    "answer",
    [
        {"option_ids": ["unknown"]},
        {"option_ids": ["web", "web"]},
        {"text": "yes"},
        {"other_text": " "},
        {"extra": "ignored?"},
    ],
)
def test_invalid_structured_answers_fail(answer: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        validate_answer(choice(), NoticeAnswer.model_validate(answer))


@pytest.mark.parametrize(
    "answer",
    [
        {"option_ids": ["web", "desktop"]},
    ],
)
def test_single_select_cannot_combine_answers(answer: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="at most one"):
        validate_answer(choice("single_select"), NoticeAnswer.model_validate(answer))


def test_legacy_other_is_not_an_answer_option() -> None:
    with pytest.raises(ValueError, match="Other is not an answer option"):
        validate_answer(choice(other=True), NoticeAnswer(other_text="Alternative"))


@pytest.mark.parametrize("kind", ["single_select", "multi_select"])
def test_a_comment_alone_is_a_valid_answer(kind: str) -> None:
    # meiva:note
    # Answering without choosing anything is allowed in both kinds (user ruling
    # 2026-10-21); the comment is then the whole answer.
    # meiva:link meiva_builtins/plugins/meiva_fleet/docs/notice-questions.meiva.okf.md
    question = choice(kind)
    answer = validate_answer(question, NoticeAnswer(comment="Please decide later"))
    assert answer.option_ids == []
    assert answer.comment == "Please decide later"
    rendered = answer_reply(question, answer)
    assert "Selected: none" in rendered
    assert "Comment: Please decide later" in rendered


@pytest.mark.parametrize("kind", ["single_select", "multi_select"])
def test_an_answer_with_neither_a_selection_nor_a_comment_is_refused(kind: str) -> None:
    with pytest.raises(ValueError, match="selection or a comment"):
        validate_answer(choice(kind), NoticeAnswer())


def test_open_answer_accepts_numbers_as_text() -> None:
    question = parse_question({"type": "text"})
    answer = parse_numbered_answer(question, "Use 2 as the baseline, then consider 1.")
    assert answer.text == "Use 2 as the baseline, then consider 1."
    assert "Answer: Use 2" in answer_reply(question, answer)
    with pytest.raises(ValueError):
        validate_answer(question, NoticeAnswer(option_ids=["web"]))


@pytest.mark.parametrize(
    "question",
    [
        {"type": "unknown"},
        {"type": "single_select", "options": []},
        {"type": "text", "options": []},
        {"type": "multi_select", "options": [{"id": "x", "label": " "}]},
        {"type": "multi_select", "options": [{"id": "x", "label": "A"}, {"id": "x", "label": "B"}]},
        {"type": "multi_select", "allow_other": "true", "options": [{"id": "x", "label": "A"}]},
    ],
)
def test_invalid_question_contracts_fail(question: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        parse_question(question)
