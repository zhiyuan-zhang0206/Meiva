from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal

_DURATION = re.compile(r"[0-9]+(?:\.[0-9]{1,9})?s\Z")
_MAX_SAFE_MS = 9_007_199_254_740_991


def duration_ms(value: object) -> int | None:
    if not isinstance(value, str) or len(value) > 40 or not _DURATION.fullmatch(value):
        return None
    milliseconds = Decimal(value[:-1]) * 1000
    return int(milliseconds) if milliseconds <= _MAX_SAFE_MS else None


# meiva:note
# Transcript step indices repeat across native records. Match a completed planner by index,
# creation second (the transcript truncates native nanoseconds) and exact stripped thinking.
# Store only the digest, never a second copy of the thought or any trajectory configuration.
# meiva:link base/lm/harness/antigravity/docs/antigravity.md
def timing_key(index: object, created_at: object, thinking: object) -> str | None:
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        return None
    if not isinstance(created_at, str) or not isinstance(thinking, str) or not thinking.strip():
        return None
    try:
        stamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return None
    try:
        second = stamp.astimezone(UTC).replace(microsecond=0).isoformat()
    except (ValueError, OverflowError):
        return None
    encoded = json.dumps([index, second, thinking.strip()], ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()
