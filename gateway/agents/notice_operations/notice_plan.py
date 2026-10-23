# meiva:note
# When a notice may exist and when it lapses, as functions of values (`now` is one of them).

from __future__ import annotations

from datetime import UTC, datetime, timedelta

__all__ = ["creation_problem", "notice_expiry", "past_expiry_problem", "question_expired"]


# meiva:note
# When a new notice lapses: the requested time, never later than the TTL limit allows.
#
# A naive request is read as UTC; none requested means the limit itself.
def notice_expiry(
    requested: datetime | None, *, now: datetime, ttl_limit_seconds: float
) -> datetime:
    limit = now + timedelta(seconds=ttl_limit_seconds)
    if requested is None:
        return limit
    aware = requested if requested.tzinfo is not None else requested.replace(tzinfo=UTC)
    return min(aware, limit)


# meiva:note
# A question that lapsed can no longer be answered.
def question_expired(expire_at: datetime, *, now: datetime) -> bool:
    return expire_at <= now


# meiva:note
# What is wrong with a requested expiry that is already behind us, or None.
def past_expiry_problem(expire_at: datetime | None, *, now: datetime) -> str | None:
    if expire_at is None:
        return None
    expiry = expire_at if expire_at.tzinfo is not None else expire_at.replace(tzinfo=UTC)
    return f"expire_at is in the past: {expiry.isoformat()}" if expiry < now else None


# meiva:note
# What makes a notice invalid before any receipt is allocated, or None.
#
# `allows_other` is true for a choice question that offers "Other" as an option.
def creation_problem(*, title: str, require_response: bool, allows_other: bool) -> str | None:
    if not title.strip():
        return "title must be non-empty"
    if not require_response:
        return "notices require a user response; send ordinary information in chat"
    if allows_other:
        return "Other is not an answer option; use a choice with an optional comment"
    return None
