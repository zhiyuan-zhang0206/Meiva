from __future__ import annotations

import re

PREVIEW_LIMIT = 140

_MARKDOWN_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MARKDOWN_NOISE = re.compile(r"(?m)^\s{0,3}(?:#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)|[*_`~]+")


# meiva:note
# The opening of the latest delivery as a banner line: links keep their text, Markdown markers go,
# whitespace collapses, and the cut lands on the limit with an ellipsis. The caller has already
# dropped the visibility prefix with its one owner (`normalize_last_message_text`). It is not
# translated: the text is the agent's own words.
# meiva:link base/events/notifications/README.md
def unread_preview(content: str) -> str:
    text = _MARKDOWN_LINK.sub(r"\1", content)
    text = _MARKDOWN_NOISE.sub("", text)
    text = " ".join(text.split())
    if len(text) <= PREVIEW_LIMIT:
        return text
    return text[: PREVIEW_LIMIT - 1].rstrip() + "…"
