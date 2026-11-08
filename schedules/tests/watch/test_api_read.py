# meiva:note
# A 502 must not read as an answer. A transient failure followed by an answer returns the answer and
# records the wait; a read that never answers raises naming the endpoint and how many attempts it
# took; a permanent status is not retried, because the retry policy belongs to the transport and this
# pins that the watch inherits it rather than a "retry everything" of its own.
# meiva:link schedules/README.md

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Literal

import pytest

from base.clock.tests.manual_timers import ManualTimers
from schedules.jobs.watch.api_read import read_api

_PATH = "repos/o/r/actions/runs/42/jobs?per_page=100"


def _answer(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 1, "", f"gh: {code}")


def _ok(body: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, body, "")


class _Gh:
    def __init__(self, *answers: subprocess.CompletedProcess[str]) -> None:
        self._answers = list(answers)
        self.argv: list[list[str]] = []

    def __call__(
        self,
        argv: Sequence[str],
        /,
        *,
        timeout: float,
        capture_output: Literal[True],
        text: Literal[True],
    ) -> subprocess.CompletedProcess[str]:
        self.argv.append(list(argv))
        return self._answers[min(len(self.argv), len(self._answers)) - 1]


def test_a_transient_answer_is_retried_and_the_read_answers() -> None:
    gh = _Gh(_answer("502 Bad Gateway"), _ok("{}"))
    timers = ManualTimers()

    assert read_api(_PATH, run_process=gh, timers=timers) == "{}"
    assert len(gh.argv) == 2
    assert timers.sleeps == [2]


def test_a_read_that_never_answers_names_the_endpoint_and_the_attempts() -> None:
    gh = _Gh(_answer("502 Bad Gateway"))
    timers = ManualTimers()

    with pytest.raises(RuntimeError) as caught:
        read_api(_PATH, run_process=gh, timers=timers)

    assert _PATH in str(caught.value)
    assert "after 4 attempt(s)" in str(caught.value)
    assert len(gh.argv) == 4
    assert timers.sleeps == [2, 4, 6]


def test_a_permanent_answer_is_not_retried() -> None:
    gh = _Gh(_answer("404 Not Found"))
    timers = ManualTimers()

    with pytest.raises(RuntimeError) as caught:
        read_api(_PATH, run_process=gh, timers=timers)

    assert "after 1 attempt(s)" in str(caught.value)
    assert timers.sleeps == []
