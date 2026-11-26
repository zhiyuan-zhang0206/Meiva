from __future__ import annotations

import re

from base.lm.harness.antigravity.acp import split_legacy_marker

# meiva:note
# AGY does not escape request tags inside user text. Only remove the outside
# frame; interior tags (including queued request separators) must remain intact
# so the text of an input is compared whole.
_REQUEST_OPEN = "<USER_REQUEST>"
_REQUEST_CLOSE = "</USER_REQUEST>"
_REQUEST_FOOTER = re.compile(
    r"\s*(?:<ADDITIONAL_METADATA>.*</ADDITIONAL_METADATA>\s*)?"
    r"(?:<USER_SETTINGS_CHANGE>.*</USER_SETTINGS_CHANGE>\s*)?",
    re.DOTALL,
)


def request_body(content: str) -> str | None:
    text = content.strip()
    if not text.startswith(_REQUEST_OPEN):
        return None
    body, closing, footer = text[len(_REQUEST_OPEN) :].rpartition(_REQUEST_CLOSE)
    if not closing or _REQUEST_FOOTER.fullmatch(footer) is None:
        return None
    return body.strip()


def user_text(content: str) -> str:
    body = request_body(content)
    return content.strip() if body is None else split_legacy_marker(body)[1]


def legacy_marker_digest(content: str) -> str | None:
    body = request_body(content)
    return None if body is None else split_legacy_marker(body)[0]


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
