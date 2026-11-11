from base.agents.questions.model import OpenQuestion
from base.agents.questions.render import (
    CONTENT_CHARS,
    LIST_CAP,
    TITLE_CHARS,
    reinjection_text,
    reminder_text,
)
from base.api_contracts.question import ChoiceQuestion, QuestionOption, QuestionType, TextQuestion


def _text(question_id: int, title: str, content: str | None = None) -> OpenQuestion:
    return OpenQuestion(
        id=question_id, title=title, content=content, question=TextQuestion(type=QuestionType.TEXT)
    )


def test_nothing_is_said_when_no_question_is_open() -> None:
    assert reminder_text([]) == ""
    assert reinjection_text([], source="x") == ""


def test_the_reminder_states_the_open_questions_the_carry_over_and_the_close_entry() -> None:
    text = reminder_text([_text(41, "Which database?"), _text(44, "Ship v2 now?")])
    assert text == (
        'Questions you asked the user that are still unanswered: #41 "Which database?", '
        '#44 "Ship v2 now?". They stay open across compaction and are shown to you again '
        "afterwards. meiva.ui.close_question(id) closes one."
    )


def test_the_reminder_cuts_long_titles_and_long_lists() -> None:
    long = [_text(i, "t" * (TITLE_CHARS + 50)) for i in range(1, LIST_CAP + 4)]
    text = reminder_text(long)
    assert "t" * TITLE_CHARS in text and "t" * (TITLE_CHARS + 1) not in text
    assert f"#{LIST_CAP} " in text and f"#{LIST_CAP + 1} " not in text
    assert "3 more are open." in text


def test_the_reinjection_lists_each_question_with_its_content_and_options() -> None:
    choice = OpenQuestion(
        id=7,
        title="Surface?",
        content="c" * (CONTENT_CHARS + 20),
        question=ChoiceQuestion(
            type=QuestionType.SINGLE_SELECT,
            options=[QuestionOption(id="web", label="Web"), QuestionOption(id="cli", label="CLI")],
        ),
    )
    text = reinjection_text([_text(3, "Why?"), choice], source="read from the database")
    assert text.startswith(
        "Questions you asked the user that are still unanswered (read from the database):"
    )
    assert '#3 "Why?"\nType: text.' in text
    assert '#7 "Surface?"' in text
    assert "c" * CONTENT_CHARS in text and "c" * (CONTENT_CHARS + 1) not in text
    assert "Type: single_select. Options:\n1. Web\n2. CLI" in text
    assert text.endswith(
        'An answer arrives as a message that starts "Re: question #<id>". '
        "meiva.ui.close_question(id) closes one."
    )
