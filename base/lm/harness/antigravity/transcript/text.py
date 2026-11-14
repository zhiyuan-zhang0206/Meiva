from __future__ import annotations

import re
from datetime import UTC, datetime

from base.lm.harness.antigravity.transcript.errors import MissingError


# meiva:note
# `created_at` is the step's creation time with second precision: it is shown (`Entry.at`) and never
# used to order anything. A value that is not a timezone-aware ISO-8601 stamp is a malformed row.
def valid_stamp(created_at: str) -> str:
    try:
        stamp = datetime.fromisoformat(created_at)
    except ValueError:
        raise MissingError("created_at") from None
    if stamp.tzinfo is None:
        raise MissingError("created_at")
    return created_at


def iso_of(clock_ms: int) -> str:
    return datetime.fromtimestamp(clock_ms // 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# meiva:note
# AGY does not escape request tags inside user text. Only remove the outside
# frame; interior tags (including queued request separators) must remain intact
# so an entire inbox message can still match as a contiguous substring.
_REQUEST_OPEN = "<USER_REQUEST>"
_REQUEST_CLOSE = "</USER_REQUEST>"
_REQUEST_FOOTER = re.compile(
    r"\s*(?:<ADDITIONAL_METADATA>.*</ADDITIONAL_METADATA>\s*)?"
    r"(?:<USER_SETTINGS_CHANGE>.*</USER_SETTINGS_CHANGE>\s*)?",
    re.DOTALL,
)


def user_text(content: str) -> str:
    text = content.strip()
    if not text.startswith(_REQUEST_OPEN):
        return text
    body, closing, footer = text[len(_REQUEST_OPEN) :].rpartition(_REQUEST_CLOSE)
    if not closing or _REQUEST_FOOTER.fullmatch(footer) is None:
        return text
    return body.strip()


# meiva:note
# A tool's output row opens with the CLI's own `Created At:` / `Completed At:` stamps, which say when
# the call ran and nothing about what it did; the row's `created_at` already carries the time.
def without_stamps(content: str) -> str:
    lines = content.splitlines()
    start = 0
    while start < len(lines) and lines[start].startswith(("Created At:", "Completed At:")):
        start += 1
    return "\n".join(lines[start:]).strip()


_BACKGROUND_DIRECTIVE = "\nYOU MUST TAKE ONE OF THE FOLLOWING TWO ACTIONS"


# meiva:note
# The row a command that moved to the background leaves says where its task and log are, then addresses
# the model ("YOU MUST TAKE ONE OF THE FOLLOWING TWO ACTIONS ..."); only the first part is the tool's.
def background_notice(content: str) -> str:
    return content.split(_BACKGROUND_DIRECTIVE, 1)[0].strip()
