# meiva:note
# When a question lapses, as a function of values (`now` is one of them).

from __future__ import annotations

from datetime import datetime

__all__ = ["question_expired"]


# meiva:note
# A question that lapsed can no longer be answered.
def question_expired(expire_at: datetime | None, *, now: datetime) -> bool:
    return expire_at is not None and expire_at <= now
