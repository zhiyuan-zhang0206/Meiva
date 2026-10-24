import asyncio

import pytest

from services.agent_runner.agent_host.lifecycle.cleanup_failures import (
    CleanupFailure,
    note_for,
    summarize,
)


def test_no_failure_raises_nothing() -> None:
    summary = summarize([])
    assert summary.primary is None
    assert summary.also_failed == ()


def test_the_first_failure_is_the_one_raised_and_the_rest_annotate_it() -> None:
    publication = RuntimeError("publication failed")
    browser = asyncio.CancelledError("browser cancelled")
    shell = RuntimeError("shell failed")
    failures = [
        CleanupFailure("publication", publication),
        CleanupFailure("browser", browser),
        CleanupFailure("shell", shell),
    ]

    summary = summarize(failures)

    assert summary.primary is publication
    assert summary.also_failed == (failures[1], failures[2])


@pytest.mark.parametrize("first", ["publication", "browser", "shell"])
def test_a_single_failure_is_raised_whichever_effect_it_came_from(first: str) -> None:
    error = RuntimeError(first)

    summary = summarize([CleanupFailure(first, error)])

    assert summary.primary is error
    assert summary.also_failed == ()


def test_a_note_names_the_step_and_the_error() -> None:
    note = note_for(CleanupFailure("shell", RuntimeError("shell failed")))

    assert note == "Post-commit shell cleanup also failed: RuntimeError('shell failed')"
