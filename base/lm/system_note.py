# meiva:note
# The tags that frame a system note in the text the model reads. Framework text the model
# must read as framework-originated — not as the human or a peer — is enclosed by one pair
# of tags: the open tag on its own line, the body, the close tag on its own line.
#
# Both halves are built here and nowhere else, so they cannot drift apart: `wrap_system_note`
# for a standalone note, and `SYSTEM_NOTE_LEAD` / `SYSTEM_NOTE_TRAIL` for a producer that has
# to bracket a body it measures (an inbound's `meiva_inbound_body_start` is the lead's length).
# `unwrap_system_note` is the reader's inverse.
#
# This leaf lives in the LLM layer because that is the layer both sides of the message stack
# already reach: `base.lm.attach` packs a note's content blocks and `base.agents.messages`
# builds the notes themselves. The agent layer importing the LLM layer is the one legal
# direction; the reverse would be a cycle.
# meiva:link agent/messages/docs/message-format.meiva.okf.md
from __future__ import annotations

__all__ = [
    "SYSTEM_NOTE_CLOSE",
    "SYSTEM_NOTE_LEAD",
    "SYSTEM_NOTE_OPEN",
    "SYSTEM_NOTE_TRAIL",
    "unwrap_system_note",
    "wrap_system_note",
]

SYSTEM_NOTE_OPEN = "<system_note>"
SYSTEM_NOTE_CLOSE = "</system_note>"
# meiva:note
# The tags with the newline that always separates them from a body, so a reader can cut a
# body out by offset without parsing the text.
# meiva:link agent/messages/docs/message-format.meiva.okf.md
SYSTEM_NOTE_LEAD = f"{SYSTEM_NOTE_OPEN}\n"
SYSTEM_NOTE_TRAIL = f"\n{SYSTEM_NOTE_CLOSE}"


def wrap_system_note(content: str) -> str:
    return f"{SYSTEM_NOTE_LEAD}{content}{SYSTEM_NOTE_TRAIL}"


def unwrap_system_note(text: str) -> str:
    # meiva:note
    # A message written before the note carried tags comes back unchanged, so a reader can
    # apply this unconditionally.
    # meiva:link agent/messages/docs/message-format.meiva.okf.md
    if not (text.startswith(SYSTEM_NOTE_LEAD) and text.endswith(SYSTEM_NOTE_TRAIL)):
        return text
    return text[len(SYSTEM_NOTE_LEAD) : -len(SYSTEM_NOTE_TRAIL)]
