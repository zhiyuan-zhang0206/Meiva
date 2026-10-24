# meiva:note
# What a native LGTM archive download waits and says between attempts, from the elapsed time and
# the attempt number alone.

from __future__ import annotations

import pytest

from cli.commands.observability.plans.lgtm.download_report import (
    attempt_failure_line,
    budget_exceeded_message,
    done_line,
    give_up_message,
    progress_line,
)
from cli.commands.observability.plans.lgtm.download_retry import (
    AttemptState,
    DownloadLimits,
    content_length,
    progress_due,
    retry_wait,
)

LIMITS = DownloadLimits(
    total_budget_s=900.0,
    socket_timeout_s=30.0,
    attempts=3,
    retry_backoff_s=5.0,
    progress_interval_s=15.0,
)


def test_the_backoff_grows_by_one_step_per_attempt() -> None:
    assert retry_wait(LIMITS, attempt=1, remaining_s=885.0) == 5.0
    assert retry_wait(LIMITS, attempt=2, remaining_s=875.0) == 10.0


def test_the_last_attempt_does_not_wait() -> None:
    assert retry_wait(LIMITS, attempt=3, remaining_s=870.0) is None


def test_a_wait_never_runs_past_the_budget_that_remains() -> None:
    assert retry_wait(LIMITS, attempt=1, remaining_s=3.0) == 3.0


@pytest.mark.parametrize("remaining_s", [0.0, -2.5])
def test_a_spent_budget_waits_zero_seconds_and_lets_the_next_attempt_fail(
    remaining_s: float,
) -> None:
    assert retry_wait(LIMITS, attempt=1, remaining_s=remaining_s) == 0.0


@pytest.mark.parametrize(
    ("header", "expected"),
    [("1048576", 1048576), (None, 0), ("", 0), ("not-a-number", None)],
)
def test_the_announced_body_size_is_read_from_the_header(
    header: str | None, expected: int | None
) -> None:
    assert content_length(header) == expected


def test_progress_is_due_once_a_full_interval_has_passed() -> None:
    assert not progress_due(LIMITS, elapsed_s=14.9, last_report_s=0.0)
    assert progress_due(LIMITS, elapsed_s=15.0, last_report_s=0.0)
    assert not progress_due(LIMITS, elapsed_s=20.0, last_report_s=15.0)


def test_the_heartbeat_shows_a_percentage_only_when_the_size_is_known() -> None:
    assert progress_line(received=2_000_000, total=10_000_000, elapsed_s=4.0) == (
        "  · lgtm native: 2.0 MB (20%) in 4s"
    )
    assert progress_line(received=2_000_000, total=0, elapsed_s=4.0) == (
        "  · lgtm native: 2.0 MB in 4s"
    )
    assert progress_line(received=2_000_000, total=None, elapsed_s=4.0) == (
        "  · lgtm native: 2.0 MB in 4s"
    )


def test_a_finished_download_reports_the_bytes_and_the_time() -> None:
    assert done_line(received=46_000_000, elapsed_s=8.4) == (
        "  · lgtm native: downloaded 46.0 MB in 8s"
    )


def test_a_failed_attempt_reports_used_time_remaining_budget_and_bytes() -> None:
    state = AttemptState(attempt=2, used_s=15.2, remaining_s=884.8, received=1024)

    assert attempt_failure_line(LIMITS, state, error="reset 2") == (
        "  ! lgtm native: download attempt 2/3 failed after 15s "
        "(885s of the 900s budget left, 1024 bytes received): reset 2"
    )


def test_a_negative_remaining_budget_is_reported_as_zero_left() -> None:
    state = AttemptState(attempt=3, used_s=902.0, remaining_s=-2.0, received=0)

    assert "(0s of the 900s budget left" in attempt_failure_line(LIMITS, state, error="late")


def test_giving_up_names_the_url_the_attempts_and_the_last_error() -> None:
    state = AttemptState(attempt=3, used_s=15.0, remaining_s=885.0, received=0)
    url = "https://example.invalid/loki.zip"

    assert give_up_message(LIMITS, state, url=url, error="reset 3") == (
        f"failed to download native LGTM backend from {url} after 3 attempt(s) "
        "(15s used of the 900s budget, 885s left, 0 bytes received by the last attempt): "
        "reset 3"
    )


def test_a_read_past_the_deadline_says_how_far_past() -> None:
    assert budget_exceeded_message(LIMITS, received=2, remaining_s=-1.04) == (
        "lgtm native download exceeded its 900s budget (2 bytes received, 1.0s past the deadline)"
    )
    assert "0.0s past the deadline" in budget_exceeded_message(LIMITS, received=0, remaining_s=0.0)
