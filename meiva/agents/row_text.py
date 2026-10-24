# meiva:note
# Relative ages describe elapsed time, not local wall-clock changes. The SDK facade supplies
# aware timestamps; this value-only formatter keeps the existing unit and negative-age rules.
# meiva:link meiva/agents/docs/relative-age.meiva.okf.md

from datetime import UTC, datetime

__all__ = ["relative_age"]


def relative_age(moment: datetime, *, now: datetime) -> str:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("moment must carry tzinfo")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must carry tzinfo")
    seconds = int((now.astimezone(UTC) - moment.astimezone(UTC)).total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"
