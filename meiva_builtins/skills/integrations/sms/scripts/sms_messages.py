# meiva:note
# The decisions of the sms skill, over values: the verification-code patterns, the
# chat.db query (SQL and parameters) for a lookback window, the mapping of the rows it
# returns to messages, and the printed lines. Reading chat.db and printing live in
# query.py.

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# meiva:note
# Apple's epoch (2001-01-01 UTC) is 978307200 seconds after the Unix epoch.
APPLE_EPOCH_OFFSET = 978307200

DEFAULT_LOOKBACK_HOURS = 12

# meiva:note
# A 20-row default page for both lenses (task #3696 exception inventory); --limit
# overrides, and the codes path over-scans internally to fill it.
DEFAULT_LIMIT = 20

_NANOSECOND_DATES_ABOVE = 1_000_000_000_000_000

# meiva:note
# The patterns of common 2FA verification codes, each capturing one group: the code digits.
# In order: Chinese, Korean and Japanese keywords, Google-style "G-123456 is your", English
# keywords, a generic "code: 123456", standalone 6-digit sequences, a bracketed code and
# "123456 is your".
_VERIFICATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:\u9a8c\u8bc1\u7801|\u6821\u9a8c\u7801|\u52a8\u6001\u7801|\u77ed\u4fe1\u9a8c\u8bc1\u7801)\D{0,5}?(\d{4,8})"
    ),
    re.compile(r"(?:\uc778\uc99d\ubc88\ud638|\uc778\uc99d\s*\ucf54\ub4dc)\D{0,5}?(\d{4,8})"),
    re.compile(
        r"(?:\u8a8d\u8a3c\u30b3\u30fc\u30c9|\u78ba\u8a8d\u30b3\u30fc\u30c9)\D{0,5}?(\d{4,8})"
    ),
    re.compile(r"G-(\d{4,8})\s+is\s+your", re.IGNORECASE),
    re.compile(
        r"(?:verification|security|one.?time|OTP|auth(?:entication)?)\s*(?:code|pin|password)\D{0,5}?(\d{4,8})",
        re.IGNORECASE,
    ),
    re.compile(r"(?:code|pin|\u5bc6\u7801)\D{0,5}?(\d{4,8})", re.IGNORECASE),
    re.compile(r"(?:^|\s)(\d{6})(?:\s|$)", re.MULTILINE),
    re.compile(r"\[(\d{4,8})\]"),
    re.compile(r"(\d{4,8})\s+is\s+your", re.IGNORECASE),
)


# meiva:note
# What one query asks for. Construction refuses a window or a page that selects nothing,
# so a limit of 0 can no longer return a row.
@dataclass(frozen=True)
class ChatFilter:
    phone_suffix: str | None
    lookback_hours: float
    limit: int
    codes_only: bool

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError(f"limit must be at least 1, got {self.limit}")
        if self.lookback_hours <= 0:
            raise ValueError(f"lookback hours must be positive, got {self.lookback_hours}")


# meiva:note
# The SELECT to run against chat.db and its bound parameters, in placeholder order.
@dataclass(frozen=True)
class ChatQuery:
    sql: str
    params: tuple[int | str, ...]


# meiva:note
# One row of the SELECT, in column order.
@dataclass(frozen=True)
class ChatRow:
    id: int
    text: str
    date: int
    is_from_me: int
    service: str
    phone: str


@dataclass(frozen=True)
class SmsMessage:
    id: int
    text: str
    phone: str
    date: str
    is_from_me: bool
    service: str
    codes: tuple[str, ...]


_SELECT = """
    SELECT
        m.ROWID AS id,
        m.text,
        m.date,
        m.is_from_me,
        m.service,
        h.id AS phone
    FROM message m
    JOIN handle h ON m.handle_id = h.ROWID
    WHERE m.service = 'SMS'
      AND m.date > ?
      AND m.text IS NOT NULL
      AND m.text != ''
"""


def _apple_epoch_to_datetime(apple_date: int) -> datetime:
    if apple_date > _NANOSECOND_DATES_ABOVE:
        unix_ts = apple_date / 1_000_000_000 + APPLE_EPOCH_OFFSET
    else:
        unix_ts = apple_date + APPLE_EPOCH_OFFSET
    return datetime.fromtimestamp(unix_ts, tz=UTC)


def _escape_like(fragment: str) -> str:
    return fragment.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# meiva:note
# The verification codes a message carries, in first-seen order across the patterns.
def extract_codes(text: str) -> list[str]:
    if not text:
        return []
    codes: list[str] = []
    seen: set[str] = set()
    for pattern in _VERIFICATION_PATTERNS:
        for match in pattern.finditer(text):
            code = match.group(1)
            if code not in seen and len(code) >= 4:
                seen.add(code)
                codes.append(code)
    return codes


# meiva:note
# The query for `filt` at time `now`: newest SMS rows inside the lookback window. A
# phone suffix matches literally (LIKE wildcards in it are escaped). When only code-bearing
# messages are wanted, a wider candidate window is scanned so the result can still reach
# `limit` of them.
def build_chat_query(filt: ChatFilter, now: datetime) -> ChatQuery:
    cutoff_dt = now - timedelta(hours=filt.lookback_hours)
    cutoff_ns = int((cutoff_dt.timestamp() - APPLE_EPOCH_OFFSET) * 1_000_000_000)
    scan_limit = max(filt.limit * 5, 50) if filt.codes_only else filt.limit
    sql = _SELECT
    params: list[int | str] = [cutoff_ns]
    if filt.phone_suffix:
        sql += " AND h.id LIKE ? ESCAPE '\\'"
        params.append(f"%{_escape_like(filt.phone_suffix)}")
    sql += " ORDER BY m.date DESC LIMIT ?"
    params.append(scan_limit)
    return ChatQuery(sql=sql, params=tuple(params))


# meiva:note
# The messages for the rows a query returned (newest first), at most `filt.limit` of them;
# a codes-only filter drops rows without a verification code.
def select_messages(rows: Sequence[ChatRow], filt: ChatFilter) -> list[SmsMessage]:
    results: list[SmsMessage] = []
    for row in rows:
        codes = extract_codes(row.text)
        if filt.codes_only and not codes:
            continue
        results.append(
            SmsMessage(
                id=row.id,
                text=row.text,
                phone=row.phone,
                date=_apple_epoch_to_datetime(row.date).isoformat(),
                is_from_me=bool(row.is_from_me),
                service=row.service,
                codes=tuple(codes),
            )
        )
        if len(results) >= filt.limit:
            break
    return results


def format_code_line(message: SmsMessage) -> str:
    return (
        f"[{message.date}] codes={' '.join(message.codes)} | {message.phone}: {message.text[:100]}"
    )


def format_message_line(message: SmsMessage) -> str:
    direction = "To" if message.is_from_me else "From"
    return f"[{message.date}] {direction} {message.phone}: {message.text[:200]}"
