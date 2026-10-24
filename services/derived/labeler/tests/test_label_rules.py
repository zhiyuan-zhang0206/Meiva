from __future__ import annotations

import pytest

from services.derived.labeler import label_rules
from services.derived.labeler.label_rules import Accepted, Empty, Rejected


def test_a_string_reply_is_its_own_text() -> None:
    assert label_rules.reply_text("Fix the labeler") == "Fix the labeler"


def test_only_text_blocks_make_the_reply() -> None:
    blocks = [
        {"type": "thinking", "thinking": "weighing", "signature": "SIG-LEAK"},
        {"type": "text", "text": "Fix"},
        {"type": "text", "text": "the labeler"},
    ]

    assert label_rules.reply_text(blocks) == "Fix the labeler"


def test_any_other_reply_shape_is_stringified() -> None:
    assert label_rules.reply_text(42) == "42"


def test_a_good_reply_is_accepted_after_normalizing() -> None:
    assert label_rules.judge('  "Fix the labeler"\nsecond line', 64) == Accepted("Fix the labeler")


def test_a_reply_is_cut_at_the_character_limit() -> None:
    assert label_rules.judge("x" * 100, 10) == Accepted("x" * 10)


@pytest.mark.parametrize("content", ["", "   \n", '""', []])
def test_a_reply_with_no_label_is_empty(content: object) -> None:
    assert isinstance(label_rules.judge(content, 64), Empty)


def test_a_reply_that_is_not_a_label_is_rejected_with_its_reason() -> None:
    assert label_rules.judge("<think>weighing the request", 64) == Rejected(
        "<think>weighing the request", "markup"
    )
    assert label_rules.judge("I'll look into it", 64) == Rejected(
        "I'll look into it", "assistant_voice"
    )


def test_the_instruction_echoed_back_is_rejected() -> None:
    echoed = label_rules.system_prompt(64)[:64]

    verdict = label_rules.judge(echoed, 64)

    assert isinstance(verdict, Rejected) and verdict.reason == "instruction_echo"


def test_the_prompt_names_the_configured_ceiling() -> None:
    assert "at most 10 characters" in label_rules.system_prompt(10)


@pytest.mark.parametrize("ceiling", [0, -5])
def test_a_ceiling_below_one_character_is_refused(ceiling: int) -> None:
    with pytest.raises(ValueError, match="labeler_max_chars"):
        label_rules.system_prompt(ceiling)


def test_rows_without_a_prompt_are_not_attempted() -> None:
    rows = [(9, "newest"), (8, None), (7, ""), (6, "oldest")]

    assert label_rules.prompts_to_attempt(rows) == [(9, "newest"), (6, "oldest")]
