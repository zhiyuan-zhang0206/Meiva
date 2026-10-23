# meiva:note
# When a notice may exist and when it lapses, asserted on values.

from datetime import UTC, datetime, timedelta, timezone

import pytest

from gateway.agents.notice_operations.notice_plan import (
    creation_problem,
    notice_expiry,
    past_expiry_problem,
    question_expired,
)

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
TTL = 7 * 24 * 3600


def test_no_requested_expiry_means_the_ttl_limit() -> None:
    assert notice_expiry(None, now=NOW, ttl_limit_seconds=TTL) == NOW + timedelta(seconds=TTL)


def test_a_requested_expiry_inside_the_limit_is_kept() -> None:
    soon = NOW + timedelta(hours=1)
    assert notice_expiry(soon, now=NOW, ttl_limit_seconds=TTL) == soon


def test_a_requested_expiry_beyond_the_limit_is_clamped() -> None:
    far = NOW + timedelta(days=30)
    assert notice_expiry(far, now=NOW, ttl_limit_seconds=TTL) == NOW + timedelta(seconds=TTL)


def test_a_naive_request_is_read_as_utc() -> None:
    # meiva:note
    # a naive request is the input.
    naive = datetime(2026, 10, 24, 13, 0)  # noqa: DTZ001
    assert notice_expiry(naive, now=NOW, ttl_limit_seconds=TTL) == naive.replace(tzinfo=UTC)


def test_the_limit_compares_instants_not_wall_clocks() -> None:
    east = timezone(timedelta(hours=8))
    same_instant_as_limit = (NOW + timedelta(seconds=TTL)).astimezone(east)
    assert notice_expiry(same_instant_as_limit, now=NOW, ttl_limit_seconds=TTL) == (
        NOW + timedelta(seconds=TTL)
    )


def test_a_question_lapses_at_its_expiry() -> None:
    assert question_expired(NOW, now=NOW) is True
    assert question_expired(NOW + timedelta(seconds=1), now=NOW) is False


def test_a_past_expiry_is_named_and_a_future_or_absent_one_is_fine() -> None:
    past = NOW - timedelta(minutes=1)
    assert past_expiry_problem(past, now=NOW) == f"expire_at is in the past: {past.isoformat()}"
    assert past_expiry_problem(NOW, now=NOW) is None
    assert past_expiry_problem(None, now=NOW) is None
    # meiva:note
    # a naive request is the input.
    naive_past = datetime(2026, 10, 24, 11, 0)  # noqa: DTZ001
    assert past_expiry_problem(naive_past, now=NOW) is not None


@pytest.mark.parametrize(
    ("title", "require_response", "allows_other", "problem"),
    [
        ("  ", True, False, "title must be non-empty"),
        ("ok", False, False, "notices require a user response; send ordinary information in chat"),
        ("ok", True, True, "Other is not an answer option; use a choice with an optional comment"),
        ("ok", True, False, None),
    ],
)
def test_creation_problems(
    title: str, require_response: bool, allows_other: bool, problem: str | None
) -> None:
    assert (
        creation_problem(title=title, require_response=require_response, allows_other=allows_other)
        == problem
    )
