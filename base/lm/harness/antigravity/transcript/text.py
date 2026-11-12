from __future__ import annotations

import re
from datetime import UTC, datetime

from base.lm.harness.antigravity.transcript.errors import MissingError


# meiva:note
# `created_at` has second precision and is the step's creation time, so it is an ordering hint and not a
# duration. It is floored to the second in milliseconds; the caller makes the sequence monotone.
def clock_ms_of(created_at: str) -> int:
    try:
        stamp = datetime.fromisoformat(created_at)
    except ValueError:
        raise MissingError("created_at") from None
    if stamp.tzinfo is None:
        raise MissingError("created_at")
    return int(stamp.astimezone(UTC).timestamp()) * 1000


def iso_of(clock_ms: int) -> str:
    return datetime.fromtimestamp(clock_ms // 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_USER_REQUEST = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)


# meiva:note
# A user row's `content` wraps what the person typed in `<USER_REQUEST>`; `<ADDITIONAL_METADATA>` (the
# local time, uploaded image paths) and `<USER_SETTINGS_CHANGE>` follow it and are the CLI's, not the
# person's. A row without the wrapper is taken whole.
def user_text(content: str) -> str:
    blocks = _USER_REQUEST.findall(content)
    return "\n\n".join(blocks).strip() if blocks else content.strip()


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
