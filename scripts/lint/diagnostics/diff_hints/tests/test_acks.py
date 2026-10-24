from __future__ import annotations

import pytest

from scripts.lint.diagnostics.diff_hints.acks import (
    answers,
    parse_acks,
    suggestions,
    unanswered,
    unused,
)
from scripts.lint.diagnostics.diff_hints.model import Ack, HintId, Site

SITE = Site(HintId.SERVICE_LOCATOR, "cli/x.py", 42, "x")
OTHER = Site(HintId.SERVICE_LOCATOR, "cli/x.py", 90, "y")


def _ack(text: str) -> Ack:
    acks, errors, _ = parse_acks(text)
    assert not errors, errors
    return acks[0]


def test_a_line_trailer_parses() -> None:
    ack = _ack(
        "subject\n\nHint-Ack: service-locator cli/x.py:42 -- composition root of the daemon\n"
    )
    assert (ack.hint, ack.path, ack.line) == (HintId.SERVICE_LOCATOR, "cli/x.py", 42)
    assert ack.reason == "composition root of the daemon"


def test_a_path_trailer_has_no_line() -> None:
    assert (
        _ack("Hint-Ack: service-locator cli/x.py -- system implementation of the port").line is None
    )


def test_git_template_comments_are_skipped() -> None:
    acks, errors, notes = parse_acks(
        "# Hint-Ack: service-locator cli/x.py -- a comment line only\n"
    )
    assert acks == () and errors == () and notes == ()


@pytest.mark.parametrize(
    "line",
    [
        "Hint-Ack: service-locator cli/x.py",
        "Hint-Ack: service-locator cli/x.py --",
        "Hint-Ack: nonsense cli/x.py -- a sentence about it",
        "Hint-Ack: ambient-read cli/x.py -- retired deterministic checker question",
        "Hint-Ack: service-locator cli/x.py -- ok",
        "Hint-Ack: service-locator cli/x.py -- <reason>",
        "Hint-Ack service-locator cli/x.py -- missing colon is not a trailer at all",
    ],
)
def test_a_malformed_trailer_is_an_error_not_an_answer(line: str) -> None:
    acks, errors, _ = parse_acks("subject\n\n" + line + "\n")
    if line.startswith("Hint-Ack:"):
        assert acks == () and len(errors) == 1
        assert "reword" in errors[0] or "reason" in errors[0] or "unknown hint id" in errors[0]
    else:
        assert acks == () and errors == ()


def test_a_line_trailer_answers_only_its_site() -> None:
    acks = (_ack("Hint-Ack: service-locator cli/x.py:42 -- composition root of the daemon"),)
    assert unanswered([SITE, OTHER], acks, ignore_line=False) == [OTHER]


def test_a_path_trailer_answers_every_site_of_the_id_in_the_file() -> None:
    acks = (_ack("Hint-Ack: service-locator cli/x.py -- composition root of the daemon"),)
    assert unanswered([SITE, OTHER], acks, ignore_line=False) == []


def test_a_trailer_for_another_id_or_file_answers_nothing() -> None:
    wrong_id = _ack("Hint-Ack: spy-assert cli/x.py -- an answer for another hint id")
    wrong_file = _ack("Hint-Ack: service-locator cli/y.py -- an answer for another file")
    assert unanswered([SITE], (wrong_id, wrong_file), ignore_line=False) == [SITE]


def test_a_directory_trailer_answers_every_site_below_it() -> None:
    acks = (_ack("Hint-Ack: service-locator cli/ -- the whole cli package is a shell layer"),)
    below = Site(HintId.SERVICE_LOCATOR, "cli/deep/z.py", 1, "z")
    outside = Site(HintId.SERVICE_LOCATOR, "gateway/z.py", 1, "z")
    assert unanswered([SITE, below, outside], acks, ignore_line=False) == [outside]


def test_branch_mode_matches_a_line_trailer_by_id_and_path() -> None:
    moved = (
        _ack("Hint-Ack: service-locator cli/x.py:7 -- written before a later commit moved it"),
    )
    assert unanswered([SITE], moved, ignore_line=True) == []
    assert unanswered([SITE], moved, ignore_line=False) == [SITE]


def test_an_ack_that_matches_no_site_is_reported_unused() -> None:
    stale = _ack("Hint-Ack: service-locator cli/gone.py:3 -- the site was fixed in a later edit")
    assert unused([stale], [SITE]) == [stale]
    assert (
        unused([_ack("Hint-Ack: service-locator cli/x.py -- composition root of daemon")], [SITE])
        == []
    )


def test_suggestions_answer_a_lone_site_by_line_and_several_by_path() -> None:
    lone = Site(HintId.SPY_ASSERT, "cli/t.py", 5, "z")
    lines = suggestions([SITE, OTHER, lone])
    assert lines == [
        "Hint-Ack: service-locator cli/x.py -- <reason>",
        "Hint-Ack: spy-assert cli/t.py:5 -- <reason>",
    ]


def test_many_files_of_one_id_collapse_into_directory_templates() -> None:
    open_sites = [
        Site(HintId.DELETED_TEST, f"cli/commands/{group}/tests/test_{i}.py", 1, "t")
        for group in ("a", "b")
        for i in range(5)
    ]
    assert suggestions(open_sites) == [
        "Hint-Ack: deleted-test cli/commands/a/tests/ -- <reason>",
        "Hint-Ack: deleted-test cli/commands/b/tests/ -- <reason>",
    ]
    wide = [Site(HintId.DELETED_TEST, f"pkg{i}/tests/test_x.py", 1, "t") for i in range(8)]
    assert len(suggestions(wide)) == 8
    deep = [Site(HintId.DELETED_TEST, f"top/{i}/sub/test_x.py", 1, "t") for i in range(8)]
    assert suggestions(deep) == ["Hint-Ack: deleted-test top/ -- <reason>"]


def test_the_key_is_case_insensitive_like_any_git_trailer_key() -> None:
    for key in ("hint-ack", "HINT-ACK", "Hint-ack"):
        ack = _ack(
            f"subject\n\n{key}: Service-Locator cli/x.py:42 -- composition root of the daemon"
        )
        assert (ack.hint, ack.line) == (HintId.SERVICE_LOCATOR, 42)


def test_narrative_that_starts_with_the_key_outside_the_trailer_block_is_a_note() -> None:
    message = (
        "Rework the poll\n\n"
        "Hint-Ack: wraps here because the text was folded and the next\n"
        "line carries the rest of the sentence.\n\n"
        "Co-Authored-By: Someone <someone@example.com>\n"
    )
    acks, errors, notes = parse_acks(message)
    assert acks == () and errors == ()
    assert len(notes) == 1 and "outside the trailer block" in notes[0]


def test_a_malformed_answer_in_the_trailer_block_says_how_to_fix_it() -> None:
    _, errors, _ = parse_acks("subject\n\nHint-Ack: service-locator cli/x.py\n")
    assert len(errors) == 1
    assert "expected `Hint-Ack: <hint-id> <path>[:<line>] -- <reason>`" in errors[0]
    assert "git commit --amend" in errors[0]


def test_a_well_formed_answer_outside_the_trailer_block_still_counts() -> None:
    message = "subject\n\nHint-Ack: service-locator cli/x.py -- composition root of the daemon\n\nbody after it\n"
    acks, errors, _ = parse_acks(message)
    assert len(acks) == 1 and errors == ()


def test_answers_counts_the_sites_an_ack_covers_by_id_and_path() -> None:
    ack = _ack("Hint-Ack: service-locator cli/ -- the whole cli package is a shell layer here")
    other = Site(HintId.SPY_ASSERT, "cli/t.py", 1, "z")
    assert answers(ack, [SITE, OTHER, other]) == 2
